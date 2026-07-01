import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.database.config import SessionLocal, initialize_database
from automation.database.models import User
from automation.auth.security import get_password_hash

initialize_database()

session = SessionLocal()
try:
    user = session.query(User).filter(User.username == 'admin').first()
    if not user:
        hashed_password = get_password_hash('admin')
        new_user = User(username='admin', password_hash=hashed_password, role='admin')
        session.add(new_user)
        session.commit()
        print('Admin user created')
    else:
        print('Admin user already exists')
except Exception as e:
    import traceback
    print('Exception during seeding:')
    traceback.print_exc()
finally:
    session.close()
