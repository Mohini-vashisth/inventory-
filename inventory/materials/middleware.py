import os
import re
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse

# Only these may be POSTed on a read-only standby: signing in and out (no data is saved by them).
ALLOWED_POSTS = (
    '/login/', '/employee-login/', '/finance-login/', '/admin-login/', '/admin/login/',
    '/admin/logout/', '/employee-logout/', '/finance-logout/', '/dashboard/logout/',
)
_BODY = re.compile(rb'<body[^>]*>', re.I)


class StandbyReadOnlyMiddleware:
    """Used only on the standby copy (STANDBY_READ_ONLY=True): refuses every change and tells
    the person they are looking at a copy. The database itself is also set to query_only in apps.py,
    so a write this misses still fails instead of being saved to the copy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.path not in ALLOWED_POSTS:
            return HttpResponse(
                '<h1>Read-only</h1><p>The main server is down, so this is a copy of the plant data. '
                'You can look at anything, but nothing can be added or changed until it is back.</p>',
                status=403,
            )
        response = self.get_response(request)
        if 'text/html' in response.get('Content-Type', '') and not response.streaming:
            try:
                as_of = datetime.fromtimestamp(os.path.getmtime(settings.DATABASES['default']['NAME'])).strftime('%d %b %Y, %H:%M')
            except OSError:
                as_of = 'an earlier backup'
            banner = (
                '<div style="background:#b45309;color:#fff;padding:8px 12px;text-align:center;font:14px sans-serif">'
                f'Read-only copy as of {as_of}. The main server is down; nothing you enter is saved.</div>'
            ).encode()
            response.content = _BODY.sub(lambda m: m.group(0) + banner, response.content, count=1)
        return response
