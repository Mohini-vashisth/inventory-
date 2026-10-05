"""Staff-only serving of uploaded files (customer POs, WhatsApp drawings)."""

from pathlib import Path
from django.conf import settings
from django.views.static import serve

from ..decorators import redirect_to_admin_login, staff_required


# Types a browser can display without executing anything. Uploads come from
# the public quote form (any file type is accepted server-side; the form's
# `accept=` attribute is browser-only), so everything else is forced to
# download rather than rendered on this app's own origin.
_INLINE_MEDIA_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.webp'}


@staff_required(on_denied=redirect_to_admin_login)
def serve_media(request, path):
    """Serves uploaded files (customer POs, WhatsApp drawings) to staff only.
    Django's static() helper can't do this: it returns nothing when
    DEBUG=False, and it would be unauthenticated anyway."""
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    if Path(path).suffix.lower() not in _INLINE_MEDIA_EXTENSIONS:
        response['Content-Disposition'] = 'attachment'
    return response
