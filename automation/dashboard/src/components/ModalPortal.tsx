import { useEffect } from 'react';
import { createPortal } from 'react-dom';

/**
 * Renders its children into <body> via a portal.
 *
 * Modals use `position: fixed` to center in the viewport, but a fixed element is
 * positioned relative to the nearest ancestor that has a transform/filter — and
 * every page here is wrapped in `.animate-fade-in`, whose fadeIn keyframe leaves a
 * non-`none` transform/filter applied. That turned "fixed to the viewport" into
 * "fixed to the (very tall) page", so modals rendered far down the page and had to
 * be scrolled to. Portaling to <body> escapes those transformed ancestors, so the
 * modal centers in the viewport again. Also locks body scroll while open and
 * closes on Escape.
 */
export default function ModalPortal({ onClose, children }: {
  onClose?: () => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose?.(); };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  return createPortal(children, document.body);
}
