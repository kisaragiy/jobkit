"""Cookie-authenticated client for this user's loopback desktop service."""
from http.cookiejar import CookieJar
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener

from local_access import LOOPBACK_HOSTS


def local_opener(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Expected a loopback HTTP origin")
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with opener.open(origin.rstrip("/") + "/", timeout=10):
        pass
    return opener
