#!/usr/bin/env python

"""
Usage:
    python googlecode2github/shadowissues.py GCPROJ GHPROJ

where "GCPROJ" is the Google Code project name, e.g. "python-markdown2"; and
"GHPROJ" is the github project id, e.g. "trentm/python-markdown2".

Limitations:
- Only supports public issues in a Google Code project. This *could* get into
  google code auth to access protected issues. Patches welcome. :)
"""

__version__ = "1.0.0"

import re
import sys
import os
from os.path import *
from glob import glob
from pprint import pprint
import codecs
import json
import operator
import datetime
from hashlib import md5
from xml.sax.saxutils import unescape as html_unescape
from xml.etree import ElementTree as ET

sys.path.insert(0, join(dirname(abspath(__file__)), "externals/lib"))
import httplib2
import appdirs



#---- globals

dirs = appdirs.AppDirs("googlecode2github", "TrentMick")



#---- primary functionality

def shadow_issues(gc_proj, gh_proj):
    print "# Gathering code.google.com/p/%s issues." % gc_proj
    gc_issues = _get_gc_issues(gc_proj)
    if not gc_issues:
        print "No code.google.com/p/%s issues found. Nothing to do." % gc_proj
    
    print "# Gathering any github.com/%s issues." % gh_proj
    gh_issues = _get_gh_issues(gh_proj)
        
    # For testing, migrate just a particular issue.
    #shadow_issue(gc_proj, gc_issues[6], gh_proj, gh_issues, force=True)
    #return
    
    # Create a GH shadow issue for each GC issue that we haven't done already.
    # - Warn (and offer to abort) if there are already issues in the GH project
    #   that are in the way.
    for i, gc_issue in enumerate(gc_issues):
        shadow_issue(gc_proj, gc_issue, gh_proj, gh_issues)

    

def shadow_issue(gc_proj, gc_issue, gh_proj, gh_issues, force=False):
    """
    Side-effect: this extends `gh_issues` if an issue is successfully added.
    
    @param force {bool} If true, this will create a shadow issue even if the
        Google Code and Github issue numbers don't match.
    """
    id = int(gc_issue["id"])
    gh_issue = [i for i in gh_issues if i["number"] == id] if gh_issues else None
    gh_new_id = ((gh_issues[-1]["number"] + 1) if gh_issues else 1)
    print "Migrating issue %s." % id
    print "     from: http://code.google.com/p/%s/issues/detail?id=%s" % (gc_proj, id)
    print "       to: https://github.com/%s/issues/%s" % (gh_proj, gh_new_id)
    #TODO: make this guard skip if title.startswith("[shadow]"), ie a previous
    #   run, but error out otherwise.
    if not force and id != gh_new_id:
        print "  WARNING: github issue id would not match, skipping"
        return
    
    #pprint(gc_issue)
    title = "[shadow] %s" % gc_issue["title"]
    extra = ""
    if gc_issue["state"] == "closed":
        extra += " Closed (%s)." % gc_issue["status"]
    if "labels" in gc_issue:
        extra += "\nLabels: %s." % ", ".join(gc_issue["labels"])
    body = u"""\
*This is a **shadow issue** for [Issue %s on Google Code](%s) (from which this project was moved).
Added %s by [%s](http://code.google.com%s).%s
Please make updates to the bug [there](%s).*

# Original description

%s
""" % (gc_issue["id"], gc_issue["url"],
       gc_issue["published"], gc_issue["author"]["name"], gc_issue["author"]["uri"], extra,
       gc_issue["url"],
       # Indent to put as Markdown pre block (because Google Code issue content
       # just isn't Markdown in general).
       _indent(gc_issue["content"]))
    #print "--"
    #print title
    #print
    #print body
    
    response, content = _github_api_post("/issues/open/%s" % gh_proj,
        {"title": title.encode('utf-8'), "body": body.encode('utf-8')})
    if response.status not in (201,):
        raise RuntimeError("unexpected response status from Github post "
            "to create issue: %s\n%s\n%s"
            % (response.status, response, content))
    new_issue = json.loads(content)["issue"]
    assert new_issue["number"] == gh_new_id, (
        "unexpected id for newly added github issue: expected %d, "
        "got %d\n--\n%s" % (gh_new_id, new_issue["number"], new_issue))
    if gc_issue["state"] == "closed":
        response, content = _github_api_post(
            "/issues/close/%s/%s" % (gh_proj, new_issue["number"]))
        if response.status not in (200,):
            raise RuntimeError("unexpected response status from Github post "
                "to close issue: %s\n%s\n%s"
                % (response.status, response, content))
        new_issue = json.loads(content)["issue"]
    gh_issues.append(new_issue)
    return new_issue

    


#---- internal support stuff

def _get_http():
    cache_dir = os.path.join(dirs.user_cache_dir, "httplib2")
    if not os.path.exists(os.path.dirname(cache_dir)):
        os.makedirs(os.path.dirname(cache_dir))
    return httplib2.Http(cache_dir)

def _load_gitconfig(path):
    from ConfigParser import ConfigParser
    import tempfile
    
    # Hack so ConfigParser can read a .gitconfig file (looser definition).
    fd_path_hack, path_hack = tempfile.mkstemp()
    content = open(path).read()
    content = re.compile(r"^\s+", re.M).sub("", content)
    f = os.fdopen(fd_path_hack, 'w')
    f.write(content)
    f.close()
    
    config = ConfigParser()
    config.read([path_hack])
    os.remove(path_hack)
    return config

_github_auth_cache = None
def _get_github_auth():
    from os.path import expanduser, exists
    from getpass import getpass
    global _github_auth_cache
    
    if _github_auth_cache is None:
        login = None
        token = None
    
        # If have them in .gitconfig (as per ngist), use that:
        #   git config --add github.user [github_username]
        #   git config --add github.token [github_api_token]
        path = expanduser("~/.gitconfig")
        if exists(path):
            config = _load_gitconfig(path)
            if config.has_option("github", "user"):
                login = config.get("github", "user")
            if config.has_option("github", "token"):
                token = config.get("github", "token")
        
        if not login:
            login = raw_input("Github username: ")
        if not token:
            token = getpass("Github API token (see <https://github.com/account#admin_bucket>): ")
        
        if not login or not token:
            raise RuntimeError("couldn't get github auth info")
        _github_auth_cache = (login, token)
    
    return _github_auth_cache

def _github_api_post(path, params=None):
    from urllib import urlencode
    import time
    
    # Hack wait to avoid hitting Github's 60 req/minute rate limiting.
    time.sleep(1)
    
    http = _get_http()
    url = "https://github.com/api/v2/json" + path
    login, token = _get_github_auth()
    if params is None:
        params = {}
    params["login"] = login
    params["token"] = token
    return http.request(url, "POST", urlencode(params))

def _get_gh_issues(gh_proj):
    """Get the issues for the given github project (only support public
    projects).

    <http://develop.github.com/p/issues.html>
    """
    issues = []
    for state in ("open", "closed"):
        http = _get_http()
        url = "https://github.com/api/v2/json/issues/list/%s/%s" % (gh_proj, state)
        response, content = http.request(url)
        if response["status"] not in ("200", "304"):
            raise RuntimeError("error GET'ing %s: %s" % (url, response["status"]))
        issues += json.loads(content)["issues"]
    issues.sort(key=operator.itemgetter("number"))
    return issues

def _get_gc_issues(gc_proj):
    """Get the Google Code issues XML for the given project.
    
    <http://code.google.com/p/support/wiki/IssueTrackerAPI>
    """
    http = _get_http()
    max_results = 1000
    url = ("https://code.google.com/feeds/issues/p/%s/" 
        "issues/full?max-results=%d" % (gc_proj, max_results))
    response, content = http.request(url)
    if response["status"] not in ("200", "304"):
        raise RuntimeError("error GET'ing %s: %s" % (url, response["status"]))
    
    feed = ET.fromstring(content)
    ns = '{http://www.w3.org/2005/Atom}'
    ns_issues = '{http://schemas.google.com/projecthosting/issues/2009}'
    
    issues = []
    for entry in feed.findall(ns+"entry"):
        alt_link = [link for link in entry.findall(ns+"link") if link.get("rel") == "alternate"][0]
        issue = {
            "title": html_unescape(entry.findtext(ns+"title")),
            "published": entry.findtext(ns+"published"),
            "updated": entry.findtext(ns+"updated"),
            "content": html_unescape(entry.findtext(ns+"content")),
            "id": entry.findtext(ns_issues+"id"),
            "url": alt_link.get("href"),
            "stars": entry.findtext(ns_issues+"stars"),
            "state": entry.findtext(ns_issues+"state"),
            "status": entry.findtext(ns_issues+"status"),
            "labels": [label.text for label in entry.findall(ns_issues+"label")],
            "author": {
                "name": entry.find(ns+"author").findtext(ns+"name"),
                "uri": entry.find(ns+"author").findtext(ns+"uri"),
            },
            #TODO: closedDate if exists
            #    <issues:closedDate>2007-11-09T05:15:25.000Z</issues:closedDate>
        }
        #pprint(issue)
        owner = entry.find(ns_issues+"owner")
        if owner is not None:
            issue["owner"] = {
                "username": entry.find(ns_issues+"owner").findtext(ns_issues+"username"),
                "uri": entry.find(ns_issues+"owner").findtext(ns_issues+"uri"),
            }
        issue["published_datetime"] = datetime.datetime.strptime(
            issue["published"], "%Y-%m-%dT%H:%M:%S.000Z")
        issue["updated_datetime"] = datetime.datetime.strptime(
            issue["updated"], "%Y-%m-%dT%H:%M:%S.000Z")
        issues.append(issue)
    #pprint(issues)
    
    if len(issues) == max_results:
        raise RuntimeError("This project might have more than %d issues and "
            "this script isn't equipped to deal with that. Aborting."
            % max_results)
    return issues
    

def log(s):
    sys.stderr.write(s+"\n")

def _indent(text):
    return '    ' + '\n    '.join(text.splitlines(False))

def _gh_page_name_from_gc_page_name(gc):
    """Github (gh) Wiki page name from Google Code (gc) Wiki page name."""
    gh = re.sub(r'([A-Z][a-z]+)', r'-\1', gc)[1:]
    return gh
    

#---- mainline


def main(argv=sys.argv):
    if len(argv) != 3:
        log("error: incorrect usage")
        log(__doc__)
        return 1
    shadow_issues(argv[1], argv[2])

if __name__ == '__main__':
    sys.exit(main(sys.argv))
