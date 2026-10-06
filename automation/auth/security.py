from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import hashlib
import os
import secrets

from automation.database.config import get_db
from automation.database.models import User

# Configuration
# APP_ENV gates every fail-closed check below. Anything that is not an explicit
# dev/test value is treated as production, so a missing or typo'd value fails
# SAFE rather than silently opening the platform up.
APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()
IS_PRODUCTION = APP_ENV not in ("dev", "development", "local", "test")

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if IS_PRODUCTION:
        # Never fall back to a constant that is committed to the repo: anyone
        # holding the source could mint valid tokens for every deployment.
        raise RuntimeError(
            "JWT_SECRET_KEY is required when APP_ENV=%s. "
            "Generate one with:  python3 -c 'import secrets; print(secrets.token_hex(32))'"
            % APP_ENV
        )
    SECRET_KEY = "dev-only-insecure-key-not-valid-in-production"

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7

import bcrypt
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

def verify_password(plain_password, hashed_password):
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_password_hash(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

def require_role(allowed_roles: list):
    def role_checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operation not permitted"
            )
        return current_user
    return role_checker


# ── Agent authentication ────────────────────────────────────────────────────
# The execution agent is a machine, not a person: it has no login and no
# refresh token, so it authenticates with a shared secret instead of a JWT.
# Without this, anything that can reach the API can claim queued jobs and post
# results for them.
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()


# Addresses that mean "this same machine". A caller from any of these is the
# local agent talking to the local backend over the loopback interface.
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})


def _is_loopback_client(request: Optional[Request]) -> bool:
    """True only when we can positively identify the caller as loopback.

    Unknown origin counts as remote, not local — an address we cannot read must
    not be treated as trusted.

    NOTE: this reads the socket peer, so it is accurate for the direct-uvicorn
    setup used here. Behind a reverse proxy every caller would look like
    loopback, and the token would have to be required unconditionally.
    """
    client = getattr(request, "client", None) if request is not None else None
    host = getattr(client, "host", None)
    return host in _LOOPBACK_CLIENTS


def require_agent(request: Request = None, x_agent_token: str = Header(default="")):
    """Guard the endpoints only the execution agent is meant to call.

    A configured AGENT_TOKEN is always enforced. With no token configured the
    rule depends on where the caller is:

      * loopback, non-production  -> allowed, so local development is unchanged
      * anything else             -> refused with a configuration error

    The backend is served with `--host 0.0.0.0` (scripts/supervise_backend.sh),
    so before this check any host on the LAN could register as an agent, claim
    queued jobs and post results for them. Local-only workflows are unaffected
    because they arrive on 127.0.0.1.
    """
    if not AGENT_TOKEN:
        if IS_PRODUCTION:
            raise HTTPException(
                status_code=503,
                detail="AGENT_TOKEN is not configured on this server.",
            )
        if not _is_loopback_client(request):
            # Never invent a token — say plainly what has to be configured.
            raise HTTPException(
                status_code=503,
                detail=(
                    "AGENT_TOKEN is not configured on this server, so agent "
                    "requests are only accepted from localhost. Set AGENT_TOKEN "
                    "on the backend and on every remote agent to allow this."
                ),
            )
        return "agent"                      # dev on loopback: unauthenticated, as before
    if not secrets.compare_digest(x_agent_token, AGENT_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return "agent"


# ── Per-agent identity (Phase 4F.1) ─────────────────────────────────────────
# AGENT_TOKEN is a BOOTSTRAP credential: it proves a caller is allowed to
# register a machine at all. It cannot answer "which machine is this?", because
# every agent holds the same value — which is why req.agent_id was never
# authoritative. Registration therefore issues a per-agent credential, and the
# backend derives identity from that instead of from the request body.

AGENT_CREDENTIAL_HEADER = "X-Agent-Credential"   # not X-Agent-Secret: that name is
                                                 # already the screen-frame shared secret


def hash_agent_credential(secret: str) -> str:
    """SHA-256 of a credential. Only the hash is ever stored."""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def issue_agent_credential() -> tuple:
    """A fresh (secret, hash) pair. The secret is returned to the agent once."""
    secret = secrets.token_urlsafe(32)
    return secret, hash_agent_credential(secret)


def authenticated_agent(
    request: Request = None,
    x_agent_credential: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """The ExecutionAgent this request actually belongs to, or None.

    Returns None when no credential is presented, so callers can keep their
    existing behaviour for a local agent that has not re-registered yet. It never
    returns an agent the caller did not prove it is: identity comes from the
    credential hash, never from a body field, a path parameter or a hostname.
    """
    from automation.database.models import ExecutionAgent

    presented = (x_agent_credential or "").strip()
    if not presented:
        return None
    try:
        return (db.query(ExecutionAgent)
                .filter(ExecutionAgent.agent_credential_hash
                        == hash_agent_credential(presented))
                .first())
    except Exception:
        return None


def require_authenticated_agent(agent=Depends(authenticated_agent)):
    """Like authenticated_agent, but refuses the request when identity is unproven.

    This is what a future /agents/me/... endpoint depends on: `me` must mean the
    machine that presented a credential, never the machine a request claims to be.
    """
    if agent is None:
        raise HTTPException(
            status_code=401,
            detail=(f"An agent credential is required. Present it as "
                    f"{AGENT_CREDENTIAL_HEADER}; agents receive one from "
                    f"/agents/register."),
        )
    return agent


def assert_agent_identity(authenticated, claimed_agent_id: Optional[str]) -> None:
    """Refuse a request whose claimed identity is not the authenticated one.

    Applied where an agent id still arrives in a body or a path. When no
    credential was presented the claim is left alone — that is the unchanged
    local-development path — but a credentialed agent can never act as another.
    """
    # `authenticated` is a resolved ExecutionAgent under FastAPI, but a Depends
    # sentinel when an endpoint function is called directly (tests, internal
    # callers). Anything without a real string id means "identity not proven",
    # which is the documented uncredentialed path — not a reason to refuse.
    authenticated_id = getattr(authenticated, "id", None)
    if not isinstance(authenticated_id, str) or not claimed_agent_id:
        return
    if claimed_agent_id != authenticated_id:
        raise HTTPException(
            status_code=403,
            detail="Agent identity mismatch: this credential belongs to a different agent.",
        )
