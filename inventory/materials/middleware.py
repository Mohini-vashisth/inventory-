import json
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
# Pages whose only purpose is entering or changing something: not opened at all on the standby, and links to them
# are greyed out. (Pages that merely show data stay open; their buttons are disabled by the script below.)
ENTRY_PAGES = (
    r'^/gate-entry/$', r'^/gate-entry/new/$', r'^/gate-entry/select/$', r'^/non-coil-entry/$',
    r'^/gate-entry/\d+/edit/$', r'^/gate-entry/\d+/add-lot/$', r'^/gate-entry/lot/\d+/coil/$',
    r'^/select-order/$', r'^/order/\d+/select-coil/$', r'^/order/\d+/pick-coil/\d+/$', r'^/scan-job/$',
    r'^/queries/\d+/edit/$', r'^/quotations/new/$', r'^/quotations/\d+/edit/$', r'^/orders/\d+/edit/$',
    r'^/finance/upload/', r'^/finance/(other|coil)/\d+/details/$', r'^/admin/.+/add/$',
)
_ENTRY = re.compile('|'.join(ENTRY_PAGES))
_ORDER_EDIT_MODE = re.compile(r'^/orders/\d+/$')
_BODY = re.compile(rb'<body[^>]*>', re.I)

# Greys out links to entry pages and disables every button that would submit a change.
_SCRIPT = '''<script>(function(){
var entry=new RegExp(%s),ok=%s;
function off(el,why){el.style.opacity='.45';el.style.cursor='not-allowed';el.title=why;}
document.querySelectorAll('form').forEach(function(f){
  if((f.method||'get').toLowerCase()!=='post'||ok.indexOf(new URL(f.action,location.href).pathname)>-1)return;
  f.querySelectorAll('button,input[type=submit],input[type=image]').forEach(function(b){b.disabled=true;off(b,'Read-only: the main server is down');});
  f.addEventListener('submit',function(e){e.preventDefault();});
});
document.querySelectorAll('a[href]').forEach(function(a){
  var u=new URL(a.href,location.href);
  if(entry.test(u.pathname)||(/^\\/orders\\/\\d+\\/$/.test(u.pathname)&&u.searchParams.has('edit'))){a.style.pointerEvents='none';off(a,'Read-only: the main server is down');}
});
})();</script>'''


class StandbyReadOnlyMiddleware:
    """Used only on the standby copy (STANDBY_READ_ONLY=True): refuses every change and tells
    the person they are looking at a copy. The database itself is also set to query_only in apps.py,
    so a write this misses still fails instead of being saved to the copy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ('GET', 'HEAD') and (
            _ENTRY.match(request.path) or (_ORDER_EDIT_MODE.match(request.path) and 'edit' in request.GET)
        ):
            return HttpResponse(
                '<h1>Not available</h1><p>This page is for entering or changing data. The main server is down, '
                'so it is switched off until it is back.</p><p><a href="javascript:history.back()">Go back</a></p>',
                status=403,
            )
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
            script = (_SCRIPT % (json.dumps('|'.join(ENTRY_PAGES)), json.dumps(list(ALLOWED_POSTS)))).encode()
            # Only look after </head>: the stylesheet in the head mentions "<body class=ds>" in a comment.
            head, sep, rest = response.content.partition(b'</head>')
            rest = _BODY.sub(lambda m: m.group(0) + banner, rest, count=1)
            response.content = head + sep + rest.replace(b'</body>', script + b'</body>', 1)
        return response
