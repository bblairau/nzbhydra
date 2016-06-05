from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import datetime
import json
import logging
import os
import urlparse
from pprint import pprint

import arrow
import jwt
from bunch import Bunch
from werkzeug.contrib.fixers import ProxyFix

from nzbhydra.searchmodules import omgwtf

sslImported = True
try:
    import ssl
except:
    sslImported = False
    print("Unable to import SSL")
import threading
import urllib
from builtins import *
from peewee import fn
from jwt import DecodeError, ExpiredSignature
from nzbhydra.exceptions import DownloaderException, IndexerResultParsingException, DownloaderNotFoundException

# standard_library.install_aliases()
from functools import update_wrapper
from time import sleep
from arrow import Arrow
from flask import Flask, render_template, request, jsonify, Response, g
from flask import redirect, make_response, send_file
from flask_cache import Cache
from flask.json import JSONEncoder
from webargs import fields
from furl import furl
from webargs.flaskparser import use_args
from werkzeug.exceptions import Unauthorized
from flask_session import Session
from nzbhydra import config, search, infos, database
from nzbhydra.api import process_for_internal_api, get_nfo, process_for_external_api, get_indexer_nzb_link, get_nzb_response, download_nzb_and_log, get_details_link, get_nzb_link_and_guid, get_entry_by_id
from nzbhydra.config import NzbAccessTypeSelection, createSecret
from nzbhydra.database import IndexerStatus, Indexer, SearchResult
from nzbhydra.downloader import getInstanceBySetting, getDownloaderInstanceByName
from nzbhydra.indexers import read_indexers_from_config, clean_up_database
from nzbhydra.search import SearchRequest
from nzbhydra.stats import get_avg_indexer_response_times, get_avg_indexer_search_results_share, get_avg_indexer_access_success, get_nzb_downloads, get_search_requests, get_indexer_statuses, getIndexerDownloadStats
from nzbhydra.update import get_rep_version, get_current_version, update, getChangelog, getVersionHistory
from nzbhydra.searchmodules.newznab import test_connection, check_caps
from nzbhydra.log import getLogs
from nzbhydra.backup_debug import backup, getDebuggingInfos, getBackupFilenames, getBackupFileByFilename
from nzbhydra import ipinfo


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        base_url = config.settings.main.urlBase
        if base_url is not None and base_url.endswith("/"):
            base_url = base_url[:-1]
        if base_url is not None and base_url != "":
            script_name = str(furl(base_url).path)
            if environ['PATH_INFO'].startswith(script_name):
                environ['PATH_INFO'] = environ['PATH_INFO'][len(script_name):]

        return self.app(environ, start_response)


logger = logging.getLogger('root')

app = Flask(__name__)
app.wsgi_app = ReverseProxied(app.wsgi_app)
app.config["SESSION_TYPE"] = "filesystem"
app.config["PRESERVE_CONTEXT_ON_EXCEPTION"] = True
app.config["PROPAGATE_EXCEPTIONS"] = True
Session(app)
flask_cache = Cache(app, config={'CACHE_TYPE': "simple", "CACHE_THRESHOLD": 250, "CACHE_DEFAULT_TIMEOUT": 60 * 60 * 24 * 7})  # Used for autocomplete and nfos and such
internal_cache = Cache(app, config={'CACHE_TYPE': "simple",  # Cache for internal data like settings, form, schema, etc. which will be invalidated on request
                                    "CACHE_DEFAULT_TIMEOUT": 60 * 30})
proxyFix = ProxyFix(app)

failedLogins = {}


def getIp():
    if not request.headers.getlist("X-Forwarded-For"):
        return request.remote_addr
    else:
        return proxyFix.get_remote_addr(request.headers.getlist("X-Forwarded-For"))


def make_request_cache_key(*args, **kwargs):
    return str(hash(frozenset(request.args.items())))


class CustomJSONEncoder(JSONEncoder):
    def default(self, obj):
        try:
            if isinstance(obj, Arrow):
                return obj.timestamp
            iterable = iter(obj)
        except TypeError:
            pass
        else:
            return list(iterable)
        return JSONEncoder.default(self, obj)


app.json_encoder = CustomJSONEncoder


@app.before_request
def _db_connect():
    if request.endpoint is not None and not request.endpoint.endswith("static"):  # No point in opening a db connection if we only serve a static file
        database.db.connect()


@app.teardown_request
def _db_disconnect(esc):
    if not database.db.is_closed():
        database.db.close()


@app.after_request
def disable_caching(response):
    if "/static" not in request.path:  # Prevent caching of control URLs
        response.cache_control.private = True
        response.cache_control.max_age = 0
        response.cache_control.must_revalidate = True
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
    response.headers["Expires"] = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    response.cache_control.max_age = 0
    user = getattr(g, "user", None)
    if user is not None:
        response.headers["Hydra-MaySeeAdmin"] = user["maySeeAdmin"]
        response.headers["Hydra-MaySeeStats"] = user["maySeeStats"]
    return response


@app.errorhandler(Exception)
def all_exception_handler(exception):
    logger.exception(exception)
    try:
        return exception.message, 500
    except:
        return "Unknwon error", 500


@app.errorhandler(422)
def handle_bad_request(err):
    # webargs attaches additional metadata to the `data` attribute
    data = getattr(err, 'data')
    if data:
        # Get validations from the ValidationError object
        messages = data['exc'].messages
    else:
        messages = ['Invalid request']

    logger.error("Invalid request: %s" % json.dumps(messages))
    return jsonify({
        'messages': messages,
    }), 422


def authenticate():
    # Only if the request actually contains auth data we consider this a login try
    if request.authorization:
        global failedLogins
        ip = getIp()

        if ip in failedLogins.keys():
            lastFailedLogin = failedLogins[ip]["lastFailedLogin"]
            lastFailedLoginFormatted = lastFailedLogin.format("YYYY-MM-DD HH:mm:ss")
            failedLoginCounter = failedLogins[ip]["failedLoginCounter"]
            lastTriedUsername = failedLogins[ip]["lastTriedUsername"]
            lastTriedPassword = failedLogins[ip]["lastTriedPassword"]
            secondsSinceLastFailedLogin = (arrow.utcnow() - lastFailedLogin).seconds
            waitFor = 2 * failedLoginCounter
            failedLogins[ip]["lastFailedLogin"] = arrow.utcnow()
            failedLogins[ip]["lastTriedUsername"] = request.authorization.username
            failedLogins[ip]["lastTriedPassword"] = request.authorization.password

            if secondsSinceLastFailedLogin < waitFor:
                if lastTriedUsername == request.authorization.username and lastTriedPassword == request.authorization.password:
                    # We don't log this and don't increase the counter, it happens when the user reloads the page waiting for the counter to go down, so we don't change the lastFailedLogin (well, we set it back)
                    failedLogins[ip]["lastFailedLogin"] = lastFailedLogin
                    return Response("Please wait %d seconds until you try to authenticate again" % (waitFor - secondsSinceLastFailedLogin), 429)
                failedLogins[ip]["failedLoginCounter"] = failedLoginCounter + 1
                logger.warn("IP %s failed to authenticate. The last time was at %s. This was his %d. failed login attempt" % (ip, lastFailedLoginFormatted, failedLoginCounter + 1))
                return Response("Please wait %d seconds until you try to authenticate again" % (waitFor - secondsSinceLastFailedLogin), 429)
            else:
                failedLogins[ip]["failedLoginCounter"] = failedLoginCounter + 1
                logger.warn("IP %s failed to authenticate. The last time was at %s. This was his %d. failed login attempt" % (ip, lastFailedLoginFormatted, failedLoginCounter + 1))

        else:
            logger.warn("IP %s failed to authenticate. This was his first failed login attempt" % ip)
            failedLogins[ip] = {"lastFailedLogin": arrow.utcnow(), "failedLoginCounter": 1, "lastTriedUsername": request.authorization.username, "lastTriedPassword": request.authorization.password}

    return Response(
        'Could not verify your access level for that URL. You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def create_token(user):
    payload = {
        'username': user.username,
        'maySeeAdmin': user.maySeeAdmin or not config.settings.auth.restrictAdmin,
        'maySeeStats': user.maySeeStats or not config.settings.auth.restrictStats,
        'iat': arrow.utcnow().datetime,
        'exp': arrow.utcnow().datetime + datetime.timedelta(days=14)
    }
    if config.settings.main.secret is None:
        logger.info("Creating secret which should've been created when migrating config. ")
        config.settings.main.secret = createSecret()
        config.save()
        
    token = jwt.encode(payload, config.settings.main.secret)
    return token.decode('unicode_escape')


def parse_token(req):
    token = req.headers.get('TokenAuthorization').split()[1]
    return jwt.decode(token, config.settings.main.secret)


# TODO: use this to create generic responses. the gui should have a service to intercept this and forward only the data (if it was successful) or else show the error, possibly log it
def create_json_response(success=True, data=None, error_message=None):
    return jsonify({"success": success, "data": data, "error_message": error_message})


def isAdminLoggedIn():
    auth = request.authorization
    return len(config.settings.auth.users) == 0 or (auth is not None and any([x.maySeeAdmin and x.username == auth.username and x.password == auth.password for x in config.settings.auth.users]))


def isAllowed(authType):
    if len(config.settings.auth.users) == 0:
        return True
    if authType == "main" and not config.settings.auth.restrictSearch:
        logger.debug("Access to main area is not restricted")
        return True
    if authType == "admin" and not config.settings.auth.restrictAdmin:
        logger.debug("Access to admin area is not restricted")
        return True
    if authType == "stats" and not config.settings.auth.restrictStats:
        logger.debug("Access to stats area is not restricted")
        return True

    if config.settings.auth.authType == "form":
        if not request.headers.get('TokenAuthorization'):
            logger.warn('Missing token authorization header')
            return False
        try:
            payload = parse_token(request)
            for u in config.settings.auth.users:
                if u.username == payload["username"]:
                    g.user = u
                    if authType == "stats":
                        maySee = u.maySeeAdmin or u.maySeeStats
                        if not maySee:
                            logger.warn("User %s may not see the stats" % u.username)
                        return maySee
                    if authType == "admin":
                        if not u.maySeeAdmin:
                            logger.warn("User %s may not see the admin area" % u.username)
                        return u.maySeeAdmin
                    return True
            else:
                logger.warn("Token is invalid, user %s is unknown" % payload["username"])
                return False
        except DecodeError:
            logger.warn('Token is invalid')
            return False
        except ExpiredSignature:
            logger.warn("Token has expired")
            return False
    else:
        auth = Bunch.fromDict(request.authorization)
        if not auth:
            logger.warn("Missing basic auth header")
            return False
        if not auth.username or not auth.password:
            logger.warn("No username or password provided")
            return False
        for u in config.settings.auth.users:
            if auth.username == u.username:
                if auth.password != u.password:
                    return False
                g.user = u
                if authType == "stats":
                    maySee = u.maySeeAdmin or u.maySeeStats
                    if not maySee:
                        logger.warn("User %s may not see the stats" % u.username)
                    return maySee
                if authType == "admin":
                    if not u.maySeeAdmin:
                        logger.warn("User %s may not see the admin area" % u.username)
                    return u.maySeeAdmin
                return True
        else:
            logger.warn("Unable to find a user with name %s" % auth.username)            
    logger.error("Auth could not be processed, this is a bug")
    return False


def requires_auth(authType, allowWithSecretKey=False, allowWithApiKey=False, isIndex=False):
    def decorator(f):
        def wrapped_function(*args, **kwargs):
            logger.debug("Call to method %s" % f.__name__)
            allowed = False
            if allowWithSecretKey and "SECRETACCESSKEY" in os.environ.keys():
                if "secretaccesskey" in request.args and request.args.get("secretaccesskey").lower() == os.environ["SECRETACCESSKEY"].lower():
                    logger.debug("Access granted by secret access key")
                    allowed = True
            elif allowWithApiKey and "apikey" in request.args:
                if request.args.get("apikey") == config.settings.main.apikey:
                    logger.debug("Access granted by API key")
                    allowed = True
                else:
                    logger.warn("API access with invalid API key from %s" % getIp())
            elif isIndex and (config.settings.auth.authType == "form" or not config.settings.auth.restrictSearch):  # Users need to be able to visit the main page without having to auth 
                allowed = True
            elif config.settings.auth.authType == "none":
                allowed = True
            else:
                logger.debug("Requiring auth for method %s" % f.__name__)
                allowed = isAllowed(authType)
            if allowed:
                try:
                    failedLogins.pop(getIp())
                    if config.settings.main.logging.logIpAddresses:
                        logger.info("Successful login from IP %s after failed login tries. Resetting failed login counter." % getIp())
                    else:
                        logger.info("Successful login from <HIDDENIP> after failed login tries. Resetting failed login counter.")
                except KeyError:
                    pass
                return f(*args, **kwargs)
            else:
                return authenticate()

        return update_wrapper(wrapped_function, f)

    return decorator


@app.route('/auth/login', methods=['POST'])
def login():
    username = request.json['username'].encode("utf-8")
    password = request.json['password'].encode("utf-8")
    for u in config.settings.auth.users:
        if u.username.encode("utf-8") == username and u.password.encode("utf-8") == password:
            token = create_token(u)
            logger.info("Form login form user %s successful" % username)
            return jsonify(token=token)
    response = jsonify(message='Wrong username or Password')
    logger.warn("Unsuccessful form login for user %s" % username)
    response.status_code = 401
    return response


@app.route('/<path:path>')
@app.route('/', defaults={"path": None})
@requires_auth("main", isIndex=True)
def base(path):
    logger.debug("Sending index.html")
    base_url = ("/" + config.settings.main.urlBase + "/").replace("//", "/") if config.settings.main.urlBase else "/"
    _, currentVersion = get_current_version()

        
    bootstrapped = {
        "baseUrl": base_url,
        "authType": config.settings.auth.authType,
        "showAdmin": not config.settings.auth.restrictAdmin or len(config.settings.auth.users) == 0 or config.settings.auth.authType == "none",
        "showStats": not config.settings.auth.restrictStats or len(config.settings.auth.users) == 0 or config.settings.auth.authType == "none",
        "maySeeAdmin": not config.settings.auth.restrictAdmin or len(config.settings.auth.users) == 0 or config.settings.auth.authType == "none",
        "maySeeStats": not config.settings.auth.restrictStats or len(config.settings.auth.users) == 0 or config.settings.auth.authType == "none",
        "maySeeSearch": not config.settings.auth.restrictSearch or len(config.settings.auth.users) == 0 or config.settings.auth.authType == "none",
    }
    if request.authorization:
        for u in config.settings.auth.users:
            if u.username == request.authorization.username:
                if config.settings.auth.restrictAdmin:
                    bootstrapped["maySeeAdmin"] = u.maySeeAdmin
                    bootstrapped["showAdmin"] = u.maySeeAdmin
                if config.settings.auth.restrictStats:
                    bootstrapped["maySeeStats"] = u.maySeeStats
                    bootstrapped["showStats"] = u.maySeeStats
    else:
        bootstrapped["showStats"] = True
        bootstrapped["showAdmin"] = True
        

    return render_template("index.html", base_url=base_url, onProd="false" if config.settings.main.debug else "true", theme=config.settings.main.theme + ".css", bootstrapped=json.dumps(bootstrapped))


def render_search_results_for_api(search_results, total, offset, output="xml"):
    if output.lower() == "xml":
        xml = render_template("api.html", channel={}, items=search_results, total=total, offset=offset)
        return Response(xml, mimetype="application/rss+xml, application/xml, text/xml")
    else:
        items = [{"title": item.title,
                  "guid": item.guid,
                  "link": item.link,
                  "comments": item.details_link,
                  "pubDate": item.pubDate,
                  "category": item.category,
                  "description": "%s - %s" % (item.title, item.indexer),
                  "enclosure": {
                      "attributes": {
                          "url": item.link,
                          "length": item.size,
                          "type": "application/x-nzb"
                      }

                  },
                  "attr": [{"@attributes": {
                      "name": attr["name"],
                      "value": attr["value"]
                  }
                           } for attr in item.attributes]

                  } for item in search_results]
        result = {"@attributes": {"version": "2.0"},
                  "channel": {
                      "title": "NZB Hydra",
                      "description": "NZB Hydra - the meta search",
                      "link": "https:\/\/github.com\/theotherp\/nzbhydra",
                      "language": "en-gb",
                      "webMaster": "TheOtherP@gmx.de (TheOtherP)",
                      "category": {},
                      "response": {
                          "@attributes": {
                              "offset": str(offset),
                              "total": str(total)
                          }
                      },
                      "items": items
                  }
                  }
        return result


externalapi_args = {
    "input": fields.String(missing=None),
    "apikey": fields.String(missing=None),
    "t": fields.String(missing=None),
    "q": fields.String(missing=None),
    "query": fields.String(missing=None),
    "group": fields.String(missing=None),
    "limit": fields.Integer(missing=100),
    "offset": fields.Integer(missing=0),
    "cat": fields.String(missing=None),
    "o": fields.String(missing="XML"),
    "raw": fields.Integer(missing=0),
    "attrs": fields.String(missing=None),
    "extended": fields.Bool(missing=None),
    "del": fields.String(missing=None),
    "rid": fields.String(missing=None),
    "genre": fields.String(missing=None),
    "imdbid": fields.String(missing=None),
    "tvdbid": fields.String(missing=None),
    "season": fields.String(missing=None),
    "ep": fields.String(missing=None),
    "id": fields.String(missing=None),
    "author": fields.String(missing=None),

    # These aren't actually needed but the way we pass args objects along we need to have them because functions check their value
    "title": fields.String(missing=None),
    "category": fields.String(missing=None),
    "episode": fields.String(missing=None),
    "minsize": fields.Integer(missing=None),
    "maxsize": fields.Integer(missing=None),
    "minage": fields.Integer(missing=None),
    "maxage": fields.Integer(missing=None),
    "dbsearchid": fields.String(missing=None),
    "indexers": fields.String(missing=None),
    "indexer": fields.String(missing=None),
    "offsets": fields.String(missing=None),
}


@app.route('/api')
@use_args(externalapi_args)
def api(args):
    logger.debug(request.url)
    logger.debug("API request: %s" % args)
    # Map newznab api parameters to internal
    args["category"] = args["cat"]
    args["episode"] = args["ep"]

    if args["q"] is not None and args["q"] != "":
        args["query"] = args["q"]  # Because internally we work with "query" instead of "q"
    if config.settings.main.apikey and ("apikey" not in args or args["apikey"] != config.settings.main.apikey):
        logger.error("Tried API access with invalid or missing API key")
        raise Unauthorized("API key not provided or invalid")
    elif args["t"] in ("search", "tvsearch", "movie", "book"):
        return api_search(args)
    elif args["t"] == "get":
        searchResultId = int(args["id"][len("nzbhydrasearchresult"):])
        searchResult = SearchResult.get(SearchResult.id == searchResultId)
        if config.settings.main.logging.logIpAddresses:
            logger.info("API request from %s to download %s from %s" % (getIp(), searchResult.title, searchResult.indexer.name))
        else:
            logger.info("API request to download %s from %s" % (searchResult.title, searchResult.indexer.name))
        return extract_nzb_infos_and_return_response(searchResultId)
    elif args["t"] == "caps":
        xml = render_template("caps.html")
        return Response(xml, mimetype="text/xml")
    elif args["t"] == "details":
        searchResultId = int(args["id"][len("nzbhydrasearchresult"):])
        searchResult = SearchResult.get(SearchResult.id == searchResultId)
        logger.info("API request from to get detils for %s from %s" % (searchResult.title, searchResult.indexer.name))
        item = get_entry_by_id(searchResult.indexer.name, searchResult.guid, searchResult.title)
        if item is None:
            logger.error("Unable to find or parse details for %s" % searchResult.title)
            return "Unable to get details", 500
        item.link = get_nzb_link_and_guid(searchResultId, False)[0]  # We need to make sure the link in the details refers to us
        return render_search_results_for_api([item], None, None, output=args["o"])
    elif args["t"] == "getnfo":
        searchResultId = int(args["id"][len("nzbhydrasearchresult"):])
        result = get_nfo(searchResultId)
        if result["has_nfo"]:
            if args["raw"] == 1:
                return result["nfo"]
            else:
                # TODO Return as json if requested
                return render_template("nfo.html", nfo=result["nfo"])
        else:
            return Response('<error code="300" description="No such item"/>', mimetype="text/xml")

    else:
        logger.error("Unknown API request. Supported functions: search, tvsearch, movie, get, caps, details, getnfo")
        return "Unknown API request. Supported functions: search, tvsearch, movie, get, caps, details, getnfo", 500


def api_search(args):
    search_request = SearchRequest(category=args["cat"], offset=args["offset"], limit=args["limit"], query=args["q"])
    if args["t"] == "search":
        search_request.type = "general"
        logger.info("")
    elif args["t"] == "tvsearch":
        search_request.type = "tv"
        identifier_key = "rid" if args["rid"] else "tvdbid" if args["tvdbid"] else None
        if identifier_key is not None:
            identifier_value = args[identifier_key]
            search_request.identifier_key = identifier_key
            search_request.identifier_value = identifier_value
        search_request.season = int(args["season"]) if args["season"] else None
        search_request.episode = int(args["episode"]) if args["episode"] else None
    elif args["t"] == "movie":
        search_request.type = "movie"
        search_request.identifier_key = "imdbid" if args["imdbid"] is not None else None
        search_request.identifier_value = args["imdbid"] if args["imdbid"] is not None else None
    elif args["t"] == "book":
        search_request.type = "ebook"
        search_request.author = args["author"] if args["author"] is not None else None
        search_request.title = args["title"] if args["title"] is not None else None
    logger.info("API search request: %s" % search_request)
    result = search.search(False, search_request)
    results = process_for_external_api(result)
    content = render_search_results_for_api(results, result["total"], result["offset"], output=args["o"])
    if args["o"].lower() == "xml":
        response = make_response(content)
        response.headers["Content-Type"] = "application/xml"
    elif args["o"].lower() == "json":
        response = jsonify(content)
    else:
        return "Unknown output format", 500

    return response


api_search.make_cache_key = make_request_cache_key


@app.route("/details/<path:guid>")
@requires_auth("main")
def get_details(guid):
    searchResultId = int(guid[len("nzbhydrasearchresult"):])
    searchResult = SearchResult.get(SearchResult.id == searchResultId)
    details_link = get_details_link(searchResult.indexer.name, searchResult.guid)
    if details_link:
        return redirect(details_link)
    return "Unable to find details", 500


searchresultid_args = {
    "searchresultid": fields.Integer()
}

internalapi__getnzb_args = {
    "searchresultid": fields.String(),
    "downloader": fields.String(missing=None)  # Name of downloader or empty if regular link
}


@app.route('/getnzb')
@requires_auth("main", allowWithApiKey=True)
@use_args(internalapi__getnzb_args)
def getnzb(args):
    logger.debug("Get NZB request with args %s" % args)
    searchResult = SearchResult.get(SearchResult.id == args["searchresultid"])
    if config.settings.main.logging.logIpAddresses:
        logger.info("API request from %s to download %s from %s" % (getIp(), searchResult.title, searchResult.indexer.name))
    else:
        logger.info("API request to download %s from %s" % (searchResult.title, searchResult.indexer.name))
    return extract_nzb_infos_and_return_response(args["searchresultid"], args["downloader"])


def process_and_jsonify_for_internalapi(results):
    if results is not None:
        results = process_for_internal_api(results)
        return jsonify(results)  # Flask cannot return lists
    else:
        return "No results", 500


def startSearch(search_request):
    results = search.search(True, search_request)
    return process_and_jsonify_for_internalapi(results)


internalapi_search_args = {
    "query": fields.String(missing=None),
    "category": fields.String(missing=None),
    "offset": fields.Integer(missing=0),
    "indexers": fields.String(missing=None),

    "minsize": fields.Integer(missing=None),
    "maxsize": fields.Integer(missing=None),
    "minage": fields.Integer(missing=None),
    "maxage": fields.Integer(missing=None)
}


@app.route('/internalapi/search')
@requires_auth("main")
@use_args(internalapi_search_args, locations=['querystring'])
def internalapi_search(args):
    logger.debug("Search request with args %s" % args)
    if args["category"].lower() == "ebook":
        type = "ebook"
    elif args["category"].lower() == "audiobook":
        type = "audiobook"
    elif args["category"].lower() == "comic":
        type = "comic"
    else:
        type = "general"
    indexers = urllib.unquote(args["indexers"]) if args["indexers"] is not None else None
    search_request = SearchRequest(type=type, query=args["query"], offset=args["offset"], category=args["category"], minsize=args["minsize"], maxsize=args["maxsize"], minage=args["minage"], maxage=args["maxage"], indexers=indexers)
    return startSearch(search_request)


internalapi_moviesearch_args = {
    "query": fields.String(missing=None),
    "category": fields.String(missing=None),
    "title": fields.String(missing=None),
    "imdbid": fields.String(missing=None),
    "tmdbid": fields.String(missing=None),
    "offset": fields.Integer(missing=0),
    "indexers": fields.String(missing=None),

    "minsize": fields.Integer(missing=None),
    "maxsize": fields.Integer(missing=None),
    "minage": fields.Integer(missing=None),
    "maxage": fields.Integer(missing=None)
}


@app.route('/internalapi/moviesearch')
@requires_auth("main")
@use_args(internalapi_moviesearch_args, locations=['querystring'])
def internalapi_moviesearch(args):
    logger.debug("Movie search request with args %s" % args)
    indexers = urllib.unquote(args["indexers"]) if args["indexers"] is not None else None
    search_request = SearchRequest(type="movie", query=args["query"], offset=args["offset"], category=args["category"], minsize=args["minsize"], maxsize=args["maxsize"], minage=args["minage"], maxage=args["maxage"], indexers=indexers)

    if args["imdbid"]:
        search_request.identifier_key = "imdbid"
        search_request.identifier_value = args["imdbid"]
    elif args["tmdbid"]:
        logger.debug("Need to get IMDB id from TMDB id %s" % args["tmdbid"])
        imdbid = infos.convertId("tmdb", "imdb", args["tmdbid"])
        if imdbid is None:
            raise AttributeError("Unable to convert TMDB id %s" % args["tmdbid"])
        search_request.identifier_key = "imdbid"
        search_request.identifier_value = imdbid

    return startSearch(search_request)


internalapi_tvsearch_args = {
    "query": fields.String(missing=None),
    "category": fields.String(missing=None),
    "title": fields.String(missing=None),
    "rid": fields.String(missing=None),
    "tvdbid": fields.String(missing=None),
    "season": fields.Integer(missing=None),
    "episode": fields.Integer(missing=None),
    "offset": fields.Integer(missing=0),
    "indexers": fields.String(missing=None),

    "minsize": fields.Integer(missing=None),
    "maxsize": fields.Integer(missing=None),
    "minage": fields.Integer(missing=None),
    "maxage": fields.Integer(missing=None)
}


@app.route('/internalapi/tvsearch')
@requires_auth("main")
@use_args(internalapi_tvsearch_args, locations=['querystring'])
def internalapi_tvsearch(args):
    logger.debug("TV search request with args %s" % args)
    indexers = urllib.unquote(args["indexers"]) if args["indexers"] is not None else None
    search_request = SearchRequest(type="tv", query=args["query"], offset=args["offset"], category=args["category"], minsize=args["minsize"], maxsize=args["maxsize"], minage=args["minage"], maxage=args["maxage"], episode=args["episode"], season=args["season"], title=args["title"],
                                   indexers=indexers)
    if args["tvdbid"]:
        search_request.identifier_key = "tvdbid"
        search_request.identifier_value = args["tvdbid"]
    elif args["rid"]:
        search_request.identifier_key = "rid"
        search_request.identifier_value = args["rid"]
    return startSearch(search_request)


internalapi_autocomplete_args = {
    "input": fields.String(missing=None),
    "type": fields.String(missing=None),
}


@app.route('/internalapi/autocomplete')
@requires_auth("main")
@use_args(internalapi_autocomplete_args, locations=['querystring'])
@flask_cache.memoize()
def internalapi_autocomplete(args):
    logger.debug("Autocomplete request with args %s" % args)
    if args["type"] == "movie":
        results = infos.find_movie_ids(args["input"])
        return jsonify({"results": results})
    elif args["type"] == "tv":
        results = infos.find_series_ids(args["input"])
        return jsonify({"results": results})
    else:
        return "No results", 500


internalapi_autocomplete.make_cache_key = make_request_cache_key


@app.route('/internalapi/getnfo')
@requires_auth("main")
@use_args(searchresultid_args)
@flask_cache.memoize()
def internalapi_getnfo(args):
    logger.debug("Get NFO request with args %s" % args)
    nfo = get_nfo(args["searchresultid"])
    return jsonify(nfo)


internalapi_getnfo.make_cache_key = make_request_cache_key


@app.route('/internalapi/getnzb')
@requires_auth("main")
@use_args(internalapi__getnzb_args)
def internalapi_getnzb(args):
    logger.debug("Get internal NZB request with args %s" % args)
    if args["downloader"] is not None:
        try:
            return extract_nzb_infos_and_return_response(args["searchresultid"], args["downloader"])
        except DownloaderNotFoundException as e:
            return e.message, 500
    else:
        return extract_nzb_infos_and_return_response(args["searchresultid"])


def extract_nzb_infos_and_return_response(searchResultId, downloader=None):
    if (downloader is None and config.settings.searching.nzbAccessType == NzbAccessTypeSelection.redirect) or (downloader is not None and getDownloaderInstanceByName(downloader).setting.nzbaccesstype == NzbAccessTypeSelection.redirect):
        link, _, _ = get_indexer_nzb_link(searchResultId, "redirect", True)
        if link is not None:
            if config.settings.main.logging.logIpAddresses:
                logger.info("Redirecting %s to %s" % (getIp(), link))
                if ipinfo.ispublic(getIp()):
                    logger.info("Info on %s: %s" % (getIp(), ipinfo.country_and_org(getIp())))
                else:
                    logger.info("Info on %s: private / RFC1918 address" % getIp())
            else:
                logger.info("Redirecting to %s" % link)

            return redirect(link)
        else:
            return "Unable to build link to NZB", 404
    else:
        return get_nzb_response(searchResultId)


internalapi__addnzb_args = {
    "searchresultids": fields.String(missing=[]),
    "category": fields.String(missing=None),
    "downloader": fields.String()  # Name of downloader
}


@app.route('/internalapi/addnzbs', methods=['GET', 'PUT'])
@requires_auth("main")
@use_args(internalapi__addnzb_args)
def internalapi_addnzb(args):
    logger.debug("Add NZB request with args %s" % args)
    searchResultIds = json.loads(args["searchresultids"])
    try:
        downloader = getDownloaderInstanceByName(args["downloader"])
    except DownloaderNotFoundException as e:
        logger.error(e.message)
        return jsonify({"success": False})
    added = 0
    for searchResultId in searchResultIds:
        try:
            searchResult = SearchResult.get(SearchResult.id == searchResultId)
        except SearchResult.DoesNotExist:
            logger.error("Unable to find search result with ID %d in database" % searchResultId)
            continue
        link = get_nzb_link_and_guid(searchResultId, True, downloader=downloader.setting.name)

        if downloader.setting.nzbAddingType == config.NzbAddingTypeSelection.link:  # We send a link to the downloader. The link is either to us (where it gets answered or redirected, thet later getnzb will be called) or directly to the indexer
            add_success = downloader.add_link(link, searchResult.title, args["category"])
        else:  # We download an NZB send it to the downloader
            nzbdownloadresult = download_nzb_and_log(searchResultId)
            if nzbdownloadresult is not None:
                add_success = downloader.add_nzb(nzbdownloadresult.content, SearchResult.get(SearchResult.id == searchResultId).title, args["category"])
            else:
                add_success = False
        if add_success:
            added += 1

    if added:
        return jsonify({"success": True, "added": added, "of": len(searchResultIds)})
    else:
        return jsonify({"success": False})


@app.route('/internalapi/test_downloader', methods=['POST'])
@requires_auth("main")
def internalapi_testdownloader():
    settings = Bunch.fromDict(request.get_json(force=True))
    logger.debug("Testing connection to downloader %s" % settings.name)
    try:
        downloader = getInstanceBySetting(settings)
        success, message = downloader.test(settings)
        return jsonify({"result": success, "message": message})
    except DownloaderNotFoundException as e:
        logger.error(e.message)
        return jsonify({"result": False, "message": e.message})


internalapi__testnewznab_args = {
    "host": fields.String(missing=None),
    "apikey": fields.String(missing=None),
}


@app.route('/internalapi/test_newznab', methods=['POST'])
@use_args(internalapi__testnewznab_args)
@requires_auth("main")
def internalapi_testnewznab(args):
    success, message = test_connection(args["host"], args["apikey"])
    return jsonify({"result": success, "message": message})


internalapi__testomgwtf_args = {
    "username": fields.String(missing=None),
    "apikey": fields.String(missing=None),
}


@app.route('/internalapi/test_omgwtf', methods=['POST'])
@use_args(internalapi__testomgwtf_args)
@requires_auth("main")
def internalapi_testomgwtf(args):
    success, message = omgwtf.test_connection(args["apikey"], args["username"])
    return jsonify({"result": success, "message": message})


internalapi_testcaps_args = {
    "indexer": fields.String(missing=None),
    "apikey": fields.String(missing=None),
    "host": fields.String(missing=None)
}


@app.route('/internalapi/test_caps', methods=['POST'])
@use_args(internalapi_testcaps_args)
@requires_auth("admin")
def internalapi_testcaps(args):
    indexer = urlparse.unquote(args["indexer"])
    apikey = args["apikey"]
    host = urlparse.unquote(args["host"])
    logger.debug("Check caps for %s" % indexer)

    try:
        ids, types = check_caps(host, apikey)
        ids = sorted(list(ids))
        types = sorted(list(types))

        return jsonify({"success": True, "ids": ids, "types": types})
    except IndexerResultParsingException as e:
        return jsonify({"success": False, "message": e.message})


@app.route('/internalapi/getstats')
@requires_auth("stats")
def internalapi_getstats():
    return jsonify({"avgResponseTimes": get_avg_indexer_response_times(),
                    "avgIndexerSearchResultsShares": get_avg_indexer_search_results_share(),
                    "avgIndexerAccessSuccesses": get_avg_indexer_access_success(),
                    "indexerDownloadShares": getIndexerDownloadStats()})


@app.route('/internalapi/getindexerstatuses')
@requires_auth("stats")
def internalapi_getindexerstatuses():
    logger.debug("Get indexer statuses")
    return jsonify({"indexerStatuses": get_indexer_statuses()})


internalapi__getnzbdownloads_args = {
    "page": fields.Integer(missing=0),
    "limit": fields.Integer(missing=100),
    "type": fields.String(missing=None)
}


@app.route('/internalapi/getnzbdownloads')
@requires_auth("stats")
@use_args(internalapi__getnzbdownloads_args)
def internalapi_getnzb_downloads(args):
    return jsonify(get_nzb_downloads(page=args["page"], limit=args["limit"], type=args["type"]))


internalapi__getsearchrequests_args = {
    "page": fields.Integer(missing=0),
    "limit": fields.Integer(missing=100),
    "type": fields.String(missing=None)
}


@app.route('/internalapi/getsearchrequests')
@requires_auth("stats")
@use_args(internalapi__getsearchrequests_args)
def internalapi_search_requests(args):
    return jsonify(get_search_requests(page=args["page"], limit=args["limit"], type=args["type"]))


internalapi__redirect_rid_args = {
    "rid": fields.String(required=True)
}


@app.route('/internalapi/redirect_rid')
@requires_auth("main")
@use_args(internalapi__redirect_rid_args)
def internalapi_redirect_rid(args):
    tvdbid = infos.convertId("tvrage", "tvdb", args["rid"])
    if tvdbid is None:
        return "Unable to find TVDB link for TVRage ID", 404
    return redirect("https://thetvdb.com/?tab=series&id=%s" % tvdbid)


internalapi__enableindexer_args = {
    "name": fields.String(required=True)
}


@app.route('/internalapi/enableindexer')
@requires_auth("stats")
@use_args(internalapi__enableindexer_args)
def internalapi_enable_indexer(args):
    logger.debug("Enabling indexer %s" % args["name"])
    indexer_status = IndexerStatus().select().join(Indexer).where(fn.lower(Indexer.name) == args["name"].lower()).get()
    indexer_status.disabled_until = 0
    indexer_status.reason = None
    indexer_status.level = 0
    indexer_status.save()
    return jsonify({"indexerStatuses": get_indexer_statuses()})


@app.route('/internalapi/setsettings', methods=["PUT"])
@requires_auth("admin")
def internalapi_setsettings():
    try:
        config.import_config_data(request.get_json(force=True))
        internal_cache.delete_memoized(internalapi_getconfig)
        internal_cache.delete_memoized(internalapi_getsafeconfig)
        read_indexers_from_config()
        clean_up_database()
        return "OK"
    except Exception as e:
        logger.exception("Error saving settings")
        return "Error: %s" % e


@app.route('/internalapi/getconfig')
@requires_auth("admin")
@internal_cache.memoize()
def internalapi_getconfig():
    return jsonify(Bunch.toDict(config.settings))


@app.route('/internalapi/getsafeconfig')
@requires_auth("main", isIndex=True)
def internalapi_getsafeconfig():
    return jsonify(config.getSafeConfig())


@app.route('/internalapi/getdebugginginfos')
@requires_auth("admin")
def internalapi_getdebugginginfos():
    try:
        debuggingInfos = getDebuggingInfos()
        if debuggingInfos is None:
            return "Error creating debugging infos", 500
        return send_file(debuggingInfos, as_attachment=True)
    except Exception as e:
        logger.exception("Error creating debugging infos")
        return "An error occured while creating the debugging infos: %s" % e, 500


@app.route('/internalapi/mayseeadminarea')
@requires_auth("main")
def internalapi_maySeeAdminArea():
    return jsonify({"maySeeAdminArea": isAdminLoggedIn()})


@app.route('/internalapi/askadmin')
@requires_auth("admin")
def internalapi_askAdmin():
    #Serves only so that the client make a request asking for admin when resolving the state change to protected tabs that don't have any other resolved calls that would trigger auth
    logger.debug("Get askadmin request")
    return "True"


@app.route('/internalapi/askforadmin')
@requires_auth("admin")
def internalapi_askforadmin():
    return "Ok... or not"


@app.route('/internalapi/get_version_history')
@requires_auth("main")
def internalapi_getversionhistory():
    versionHistory = getVersionHistory()
    return jsonify({"versionHistory": versionHistory})


internalapi__getChangelog_args = {
    "currentVersion": fields.String(required=True),
    "repVersion": fields.String(required=True)
}


@app.route('/internalapi/get_changelog')
@requires_auth("main")
@use_args(internalapi__getChangelog_args)
def internalapi_getchangelog(args):
    _, current_version_readable = get_current_version()
    changelog = getChangelog(args["currentVersion"], args["repVersion"])
    return jsonify({"changelog": changelog})


@app.route('/internalapi/get_versions')
@requires_auth("main")
def internalapi_getversions():
    current_version, current_version_readable = get_current_version()
    rep_version, rep_version_readable = get_rep_version()

    versionsInfo = {"currentVersion": str(current_version_readable), "repVersion": str(rep_version_readable), "updateAvailable": rep_version > current_version}

    if rep_version > current_version:
        changelog = getChangelog(current_version_readable, rep_version_readable)
        versionsInfo["changelog"] = changelog

    return jsonify(versionsInfo)


@app.route('/internalapi/getlogs')
@requires_auth("admin")
def internalapi_getlogs():
    logs = getLogs()
    return jsonify(logs)


@app.route('/internalapi/getbackup')
@requires_auth("admin")
def internalapi_getbackup():
    backupFile = backup()
    if backupFile is None:
        return "Error creating backup file", 500
    return send_file(backupFile, as_attachment=True, mimetype="application/zip")


internalapi_getbackupfile_args = {
    "filename": fields.String(required=True)
}


@app.route('/internalapi/getbackupfile')
@requires_auth("admin")
@use_args(internalapi_getbackupfile_args)
def internalapi_getbackupfile(args):
    backupFile = getBackupFileByFilename(args["filename"])
    logger.info("Sending %s" % backupFile)
    return send_file(backupFile, as_attachment=True, mimetype="application/zip")


@app.route('/internalapi/getbackups')
@requires_auth("admin")
def internalapi_getbackups():
    logger.debug("Get backups request")
    return jsonify({"backups": getBackupFilenames()})


internalapi_getbackupfile_args = {
    "error": fields.String(required=True),
    "cause": fields.String(required=True)
}


@app.route('/internalapi/logerror', methods=['GET', 'PUT'])
@requires_auth("main")
@use_args(internalapi_getbackupfile_args)
def internalapi_logerror(args):
    logger.error("The client encountered the following error: %s. Caused by: %s" % (args["error"], args["cause"]))
    return "OK"


internalapi__downloader_args = {
    "downloader": fields.String(required=True)  # the name
}


@app.route('/internalapi/getcategories')
@requires_auth("main")
@use_args(internalapi__downloader_args)
def internalapi_getcategories(args):
    try:
        downloader = getDownloaderInstanceByName(args["downloader"])
        return jsonify({"success": True, "categories": downloader.get_categories()})
    except DownloaderNotFoundException as e:
        logger.error(e.message)
        return jsonify({"success": False, "message": e.message})
    except DownloaderException as e:
        return jsonify({"success": False, "message": e.message})


@app.route('/internalapi/gettheme')
@requires_auth("main")
def internalapi_gettheme():
    return send_file("../static/css/default.css")


def restart(func=None, afterUpdate=False):
    logger.info("Restarting now")
    logger.debug("Setting env variable RESTART to 1")
    os.environ["RESTART"] = "1"
    if afterUpdate:
        logger.debug("Setting env variable AFTERUPDATE to 1")
        os.environ["AFTERUPDATE"] = "1"
    logger.debug("Sending shutdown signal to server")
    func()


@app.route("/internalapi/restart")
@requires_auth("admin", True)
def internalapi_restart():
    logger.info("User requested to restart. Sending restart command in 1 second")
    func = request.environ.get('werkzeug.server.shutdown')
    thread = threading.Thread(target=restart, args=(func, False))
    thread.daemon = True
    thread.start()
    return "Restarting"


def shutdown():
    logger.debug("Sending shutdown signal to server")
    sleep(1)
    os._exit(0)


@app.route("/internalapi/shutdown")
@requires_auth("admin", True)
def internalapi_shutdown():
    logger.info("Shutting down due to external request")
    thread = threading.Thread(target=shutdown)
    thread.daemon = True
    thread.start()
    return "Shutting down..."


@app.route("/internalapi/update")
@requires_auth("admin")
def internalapi_update():
    logger.info("Starting update")
    updated = update()
    if not updated:
        return jsonify({"success": False})
    logger.info("Will send restart command in 1 second")
    func = request.environ.get('werkzeug.server.shutdown')
    thread = threading.Thread(target=restart, args=(func, True))
    thread.daemon = True
    thread.start()
    return jsonify({"success": True})


def run(host, port, basepath):
    # type: (str, int, str) -> object
    context = create_context()
    configureFolders(basepath)
    for handler in logger.handlers:
        app.logger.addHandler(handler)
    if context is None:
        app.run(host=host, port=port, debug=config.settings.main.debug, threaded=config.settings.main.runThreaded, use_reloader=config.settings.main.flaskReloader)
    else:
        app.run(host=host, port=port, debug=config.settings.main.debug, ssl_context=context, threaded=config.settings.main.runThreaded, use_reloader=config.settings.main.flaskReloader)


def configureFolders(basepath):
    app.template_folder = os.path.join(basepath, "templates")
    app.static_folder = os.path.join(basepath, "static")


def create_context():
    context = None
    if config.settings.main.ssl:
        if not sslImported:
            logger.error("SSL could not be imported, sorry. Falling back to standard HTTP")
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.load_cert_chain(config.settings.main.sslcert, config.settings.main.sslkey)
    return context
