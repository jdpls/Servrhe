﻿#!/usr/bin/env python
# -*- coding: utf-8 -*-

LOCAL = False

# RHE-proofing. Auto-installs dependencies for you!

# Pip
try:
    import pip
except ImportError, e:
    import urllib2
    exec urllib2.urlopen("http://python-distribute.org/distribute_setup.py").read()
    exec urllib2.urlopen("https://raw.github.com/pypa/pip/master/contrib/get-pip.py").read()

# Everything else
try:
    import twisted, watchdog
except ImportError, e:
    from pip.index import PackageFinder
    from pip.req import InstallRequirement, RequirementSet
    from pip.locations import build_prefix, src_prefix
     
    requirement_set = RequirementSet(
        build_dir=build_prefix,
        src_dir=src_prefix,
        download_dir=None)
     
    requirement_set.add_requirement( InstallRequirement.from_line("twisted", None) )
    requirement_set.add_requirement( InstallRequirement.from_line("watchdog", None) )
     
    install_options = []
    global_options = []
    finder = PackageFinder(find_links=[], index_urls=["http://pypi.python.org/simple/"])
     
    requirement_set.prepare_files(finder, force_root_egg_info=False, bundle=False)
    requirement_set.install(install_options, global_options)

# Actual bot stuff
from twisted.internet import reactor, protocol, task, utils, defer
from twisted.words.protocols import irc
from twisted.web.client import Agent, FileBodyProducer
from twisted.web.http_headers import Headers
from StringIO import StringIO
from functools import wraps
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import re, urllib, json, os, inspect, datetime, fnmatch, ctypes, sys

logs = []
def log(*params):
    m = " ".join([repr(x) for x in params])
    print m
    logs.append(m)
    while len(logs) > 20:
        logs.pop(0)

def public(fn):
    @wraps(fn)
    def wrapped(self, user, *params, **kwargs):
        return fn(self, user, *params, **kwargs)
    wrapped._admin_required = False
    return wrapped

def admin(fn):
    @wraps(fn)
    def wrapped(self, user, *params, **kwargs):
        if user.lower() not in self.factory.admins:
            return
        return fn(self, user, *params, **kwargs)
    wrapped._admin_required = True
    return wrapped

def owner(fn):
    @wraps(fn)
    def wrapped(self, user, *params, **kwargs):
        if user.lower() not in ("fugiman","rhexcelion"):
            return
        return fn(self, user, *params, **kwargs)
    wrapped._admin_required = True
    return wrapped

def dt2ts(dt):
    hours = dt.seconds // 3600
    minutes = (dt.seconds // 60) % 60
    seconds = dt.seconds % 60
    when = ""
    if dt.days:
        when = "%d days, %d hours, %d minutes and %d seconds" % (dt.days, hours, minutes, seconds)
    elif hours:
        when = "%d hours, %d minutes and %d seconds" % (hours, minutes, seconds)
    elif minutes:
        when = "%d minutes and %d seconds" % (minutes, seconds)
    else:
        when = "%d seconds" % seconds
    return when

def bytes2human(num):
    for x in ['bytes','KB','MB','GB']:
        if num < 1024.0:
            return "%3.1f%s" % (num, x)
        num /= 1024.0
    return "%3.1f%s" % (num, 'TB')

class BodyStringifier(protocol.Protocol):
    def __init__(self, deferred):
        self.deferred = deferred
        self.buffer = ""
    def dataReceived(self, data):
        self.buffer += data
    def connectionLost(self, reason):
        self.deferred.callback(self.buffer)

def fetchPage(url, data=None, headers={}):
    method = "POST" if data else "GET"
    body = FileBodyProducer(StringIO(data)) if data else None
    d = Agent(reactor).request(method, url, Headers(headers), body)
    def handler(response):
        if response.code == 204:
            d = defer.succeed("")
        else:
            d = defer.Deferred()
            response.deliverBody(BodyStringifier(d))
        return d
    d.addCallback(handler)
    return d

def getPath(cmd):
    if os.path.isabs(cmd):
        return cmd
    exts = [""] if "." in cmd else ["",".exe",".bat"]
    paths = filter(lambda x: x, os.environ["PATH"].replace("\\\\","/").split(os.pathsep))
    for p in paths:
        for e in exts:
            r = os.path.join(p, cmd) + e
            if os.path.isfile(r):
                return r
    raise Exception("No valid path found for "+cmd)

# register winapi functions
if LOCAL:
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId

class ServrheDCC(irc.DccFileReceive):
    # Small wrapper so we know when the file is done
    def connectionLost(self, reason):
        irc.DccFileReceive.connectionLost(self, reason)
        if self.bytesReceived == self.fileSize:
            self.factory.master.rip_success(self.filename)
        else:
            self.factory.master.rip_fail(self.filename)

class ServrheDCCFactory(protocol.ClientFactory):
    protocol = ServrheDCC
    def __init__(self, filename, size, data, dest):
        self.filename = filename
        self.size = size
        self.data = data
        self.dest = dest
        self.overwrite = False
        self.protocols = []
    
    def buildProtocol(self, addr):
        p = self.protocol(self.filename, self.size, self.data, self.dest)
        p.factory = self
        p.set_overwrite(self.overwrite)
        self.protocols.append(p)
        return p
    
    def set_overwrite(self, overwrite):
        self.overwrite = overwrite
        for p in self.protocols:
            p.set_overwrite(self.overwrite)

class ServrheObserver(FileSystemEventHandler):
    def __init__(self, factory, reactor, base_dir):
        self.factory = factory
        self.reactor = reactor
        self.base_dir = base_dir
        super(FileSystemEventHandler, self).__init__()

    def on_thread_told_to_stop():
        self.factory.observer_running = False

    def on_created(self, event):
        if event.is_directory or not LOCAL:
            return
        self.reactor.callFromThread(self.factory.file_change, event.src_path.replace(self.base_dir, ""), bytes2human(os.path.getsize(event.src_path)))

class Servrhe(irc.IRCClient):
    nickname = "ServrheV2"
    rips = False
    bots = False
    
    # Bot maintenance
    def signedOn(self):
        self.msg("NickServ","IDENTIFY %s" % self.factory.password)
        self.rips = {}
        self.bots = {}
        self.factory.resetDelay()
        self.factory.protocols.append(self)
        for c in self.factory.channels:
            self.join(str(c))
    
    def connectionLost(self, reason=None):
        self.factory.protocols.remove(self)
    
    def privmsg(self, user, channel, msg):
        user = user.split("!", 1)[0]
        channel = channel if channel != self.nickname else user
        if not msg.startswith("."): # not a trigger command
            return # do nothing
        command, sep, rest = msg.lstrip(".").partition(" ")
        msg = filter(lambda x: x, rest.split(" "))
        func = getattr(self, "cmd_%s" % command.lower(), None)
        if func is not None:
            log(user, channel, command, msg)
            func(user, channel, msg)

    def msg(self, channel, message):
        irc.IRCClient.msg(self, channel, unicode(message).encode("utf-8"))

    def notice(self, user, message):
        irc.IRCClient.notice(self, user, unicode(message).encode("utf-8"))
    
    # Public commands
    @public
    def cmd_man(self, user, channel, msg):
        """.man [command] [command] ... || .man man commands || Gives usage and description of commands"""
        if not msg:
            msg = ["man"]
        for cmd in msg:
            func = getattr(self, "cmd_%s" % cmd.lower(), None)
            if func is not None:
                self.msg(channel, inspect.getdoc(func))
    
    @public
    def cmd_commands(self, user, channel, msg):
        """.commands || .commands || Lists available commands"""
        admin = user.lower() in self.factory.admins
        r = ["Available commands:"]
        methods = filter(lambda x: x[0][:4] == "cmd_", inspect.getmembers(self))
        for m in methods:
            try:
                admin_required = m[1]._admin_required
            except:
                continue
            else:
                if admin_required and not admin:
                    continue
                r.append(m[0][4:])
        self.msg(channel, " ".join(r))
    
    @public
    @defer.inlineCallbacks
    def cmd_blame(self, user, channel, msg):
        """.blame [show name] || .blame Accel World || Reports who is to blame for a show not being released"""
        dt = datetime.datetime
        show = self.factory.resolve(" ".join(msg), channel)
        if show is None:
            return
        data = yield self.factory.load("show",show["id"],"substatus")
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        data = data["results"]
        if data["position"] in ["encoder","translator"]:
            data["updated"] = show["airtime"] + 30*60 # Airtime + 30min, adjusts for completion of airing
        updated = dt.utcnow() - dt.utcfromtimestamp(data["updated"])
        when = dt2ts(updated)
        if data["position"] == "completed" and data["value"] == "completed":
            self.msg(channel, "%s is completed as of %s ago" % (show["series"], when))
        else:
            self.msg(channel, "%s is at the %s, %s, as of %s ago" % (show["series"], data["position"], data["value"], when))
    
    @public
    @defer.inlineCallbacks
    def cmd_whodoes(self, user, channel, msg):
        """.whodoes [position] [show name] || .whodoes timer Accel World || Reports who does a job for a show"""
        position = msg[0]
        if position not in self.factory.positions:
            self.msg(channel, "%s is not a valid position. Try %s, or %s." % (msg[0], ", ".join(self.factory.positions[:-1]), self.factory.positions[-1]))
            return
        show = self.factory.resolve(" ".join(msg[1:]), channel)
        if show is None:
            return
        data = yield self.factory.load("show",show["id"],position)
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        data = data["results"]
        self.msg(channel, "%s is the %s for %s" % (data["name"], data["position"], show["series"]))
    
    @public
    def cmd_mariafuckingwhere(self, user, channel, msg):
        """.mariafuckingwhere || .mariafuckingwhere || Tells you where Maria fucking is"""
        dt = datetime.date
        times = {"8":dt(2013,1,6),"9":dt(2013,5,2),"10":dt(2013,8,31),"11":dt(2014,1,5),"12":dt(2014,5,18),"13":dt(2014,10,11)}
        show = self.factory.resolve("Maria", channel)
        if show is None:
            return
        ep = int(show["current_ep"]) + 1
        when = times[str(ep)] - dt.today()
        self.msg(channel, "%s %d will be released in %d days" % (show["series"], ep, when.days))
    
    @public
    def cmd_airing(self, user, channel, msg):
        """.airing || .airing || Lists the shows airing in the next 24 hours"""
        dt = datetime.datetime
        now = dt.utcnow()
        shows = []
        ret = []
        for show in self.factory.shows.itervalues():
            if show["current_ep"] == show["total_eps"]:
                continue
            diff = dt.utcfromtimestamp(show["airtime"]) - now
            if diff.days == 0:
                shows.append((diff,show["series"]))
        shows.sort(key=lambda s: s[0])
        for s in shows:
            self.msg(channel, "%s airs in %s" % (s[1], dt2ts(s[0])))
    
    @public
    @defer.inlineCallbacks
    def cmd_aired(self, user, channel, msg):
        """.aired || .aired || Lists the shows aired but not encoded"""
        data = yield self.factory.load("shows","aired_compact")
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        data = data["results"]
        shows = ["{} {:d}".format(d["series"], d["current_ep"]+1) for d in data]
        if shows:
            self.msg(channel, "Waiting for encode: "+", ".join(shows))
        else:
            self.msg(channel, "No shows awaiting encoding")
    
    # Admin commands
    @admin
    def cmd_join(self, user, channel, msg):
        """.join [channel] [channel] ... || .join #commie-subs || Makes the bot join channels"""
        for c in msg:
            self.join(c)
            self.factory.channels.append(c)
    
    @admin
    def cmd_leave(self, user, channel, msg):
        """.leave [channel] [channel] ... || .leave #commie-subs || Makes the bot leave channels"""
        for c in msg:
            self.leave(c)
            self.factory.channels.remove(c)
    
    @admin
    def cmd_admin(self, user, channel, msg):
        """.admin [user] [user] ... || .admin Fugiman || Gives users access to admin commands"""
        self.factory.admins = list(set(self.factory.admins).union(set([c.lower() for c in msg])))
        self.factory.admins.sort()
        self.notice(user, " ".join(self.factory.admins))
    
    @admin
    def cmd_unadmin(self, user, channel, msg):
        """.unadmin [user] [user] ... || .unadmin Fugiman || Revokes users' access to admin commands"""
        self.factory.admins = list(set(self.factory.admins).difference(set([c.lower() for c in msg])))
        self.factory.admins.sort()
        self.notice(user, " ".join(self.factory.admins))
    
    @admin
    def cmd_listadmins(self, user, channel, msg):
        """.listadmins || .listadmins || Prints the list of users with access to admin commands"""
        self.notice(user, " ".join(self.factory.admins))
    
    @admin
    def cmd_setbots(self, user, channel, msg):
        """.setbots [bot] [bot] ... || .setbots Arutha Cerebrate || Sets the botlist for the rip script"""
        self.factory.bots = [m.lower() for m in msg]
        self.msg(channel, "Bots: "+", ".join(self.factory.bots))
    
    @admin
    @defer.inlineCallbacks
    def cmd_assign(self, user, channel, msg):
        """.assign [position] [victim] [show name] || .assign timer Fugiman Accel World || Assigns the victim to the position for the show"""
        position = msg[0]
        if position not in self.factory.positions:
            self.msg(channel, "%s is not a valid position. Try %s, or %s." % (msg[0], ", ".join(self.factory.positions[:-1]), self.factory.positions[-1]))
            return
        victim = msg[1]
        show = self.factory.resolve(" ".join(msg[2:]), channel)
        if show is None:
            return
        data = {"id":show["id"],"method":"position","position":position,"value":victim}
        data = yield self.factory.load("show","update", data=data)
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        self.msg(channel, "%s for %s is assigned to %s" % (position, show["series"], victim))
    
    @admin
    @defer.inlineCallbacks
    def cmd_done(self, user, channel, msg, done = True):
        """.done [position] [show name] || .done timer Accel World || Marks a position for a show as done"""
        position = msg[0]
        if position not in self.factory.positions:
            self.msg(channel, "%s is not a valid position. Try %s, or %s." % (msg[0], ", ".join(self.factory.positions[:-1]), self.factory.positions[-1]))
            return
        show = self.factory.resolve(" ".join(msg[1:]), channel)
        if show is None:
            return
        data = {"id":show["id"],"method":"position_status","position":position,"value":"1" if done else "0"}
        data = yield self.factory.load("show","update", data=data)
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        self.msg(channel, "%s for %s is marked as %s" % (position, show["series"], "done" if done else "not done"))
    
    @admin
    def cmd_undone(self, user, channel, msg):
        """.undone [position] [show name] || .undone timer Accel World || Marks a position for a show as not done"""
        self.cmd_done(user, channel, msg, False)
    
    @admin
    @defer.inlineCallbacks
    def cmd_finished(self, user, channel, msg):
        """.finished [show name] || .finished Accel World || Marks a show as released"""
        show = self.factory.resolve(" ".join(msg), channel)
        if show is None:
            return
        data = {"id":show["id"],"method":"next_episode"}
        data = yield self.factory.load("show","update", data=data)
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        self.msg(channel, "%s is marked as completed for the week" % show["series"])
        self.factory.update_topic()
    
    @admin
    @defer.inlineCallbacks
    def cmd_unfinished(self, user, channel, msg):
        """.unfinished [show name] || .unfinished Accel World || Reverts the show to last week and marks as not released"""
        show = self.factory.resolve(" ".join(msg), channel)
        if show is None:
            return
        data = {"id":show["id"],"method":"restart_last_episode"}
        data = yield self.factory.load("show","update", data=data)
        if "status" in data and not data["status"]:
            self.msg(channel, data["message"])
            return
        self.msg(channel, "%s is reverted to last week" % show["series"])
    
    @admin
    def cmd_ar(self, user, channel, msg):
        """.ar [show name] || .ar Accel World || Adds show to list of shows needing to be released."""
        show = self.factory.resolve(" ".join(msg), channel)
        if show is None:
            return
        self.factory.releases.append(show["series"])
        self.msg(channel, "%s added to release list" % show["series"])
    
    @admin
    def cmd_lr(self, user, channel, msg):
        """.lr || .lr || Lists shows needing to be released."""
        self.msg(channel, "Pending: "+", ".join(self.factory.releases))
    
    @admin
    def cmd_dr(self, user, channel, msg):
        """.dr || .dr || Clears list of shows needing to be released."""
        message = "Did you remember to update the topic? Cleared: "+", ".join(self.factory.releases)
        self.factory.releases = []
        self.msg(channel, message)
    
    
    @admin
    def cmd_ah(self, user, channel, msg):
        """.ah [show name] [name] ... || .ah accel* Fugiman || Adds names to a highlight list upon file changes."""
        if not LOCAL:
            return

        if not msg:
            return self.msg(channel, 'No show given to add highlights to')
        show = msg.pop(0)
        if not msg:
            return self.msg(channel, 'No names given to highlight for "{}"'.format(show))
        if not show in self.factory.highlights:
            self.factory.highlights[show] = []
        self.factory.highlights[show].extend(msg)
        self.msg(channel, 'Highlights for "{}" are now: {}'.format(show, " ".join(self.factory.highlights[show])))
    
    @admin
    def cmd_lh(self, user, channel, msg):
        """.lh [show name] || .lh accel* || Lists highlights for a given show."""
        if not LOCAL:
            return

        if not msg:
            return self.msg(channel, 'No show given to find highlights for')
        show = msg.pop(0)
        message = []
        for key, people in self.factory.highlights.items():
            if fnmatch.fnmatch(key, show):
                message.append("{}: {}".format(key, " ".join(people)))
        if not message:
            return self.msg(channel, 'No highlights for "{}"'.format(show))
        for m in message:
            self.msg(channel, m)
    
    @admin
    def cmd_dh(self, user, channel, msg):
        """.dh [showname] [name] ... || .dh accel* Fugiman || Deletes names from a highlight list."""
        if not LOCAL:
            return

        if not msg:
            return self.msg(channel, 'No show given to delete highlights from')
        show = msg.pop(0)
        if not show in self.factory.highlights:
            return self.msg(channel, 'Show "{}" has no highlights'.format(show))
        if msg:
            deleted = []
            for p in self.factory.highlights[show]:
                if p in msg:
                    self.factory.highlights[show].remove(p)
                    deleted.append(p)
            if not deleted:
                self.msg(channel, 'No highlights deleted for "{}"'.format(show))
            else:
                self.msg(channel, 'Deleted the following highlights from "{}": {}'.format(show, " ".join(deleted)))
        else:
            del self.factory.highlights[show]
            self.msg(channel, 'Deleted all highlights for "{}"'.format(show))

    @admin
    def cmd_topiclimit(self, user, channel, msg):
        """.topiclimit [limit] || .topiclimit 20 || Sets the max number of shows to display in the topic"""
        if not msg:
            return self.msg(channel, "No limit given")
        try:
            self.factory.topic[1] = int(msg[0])
            self.factory.update_topic()
        except ValueError:
            self.msg(channel, "Invalid limit (must be an int)")

    @admin
    def cmd_topicpercent(self, user, channel, msg):
        """.topicpercent [percentage] || .topicpercent 100.00 || Sets the Mahoyo progress percentage"""
        if not msg:
            return self.msg(channel, "No percentage given")
        try:
            self.factory.topic[2] = float(msg[0])
            self.factory.update_topic()
        except ValueError:
            self.msg(channel, "Invalid percentage (must be a float)")

    @admin
    def cmd_topicadd(self, user, channel, msg):
        """.topicadd [contents] || .topicadd Some Faggotry Here || Adds some text to the end of the topic"""
        if not msg:
            return self.msg(channel, "No message given")
        self.factory.topic.append(" ".join(msg))
        self.factory.update_topic()

    @admin
    def cmd_topicclear(self, user, channel, msg):
        """.topicclear || .topicclear || Clears all text from the end of the topic"""
        self.factory.topic = self.factory.topic[:3]
        self.factory.update_topic()

    @admin
    @defer.inlineCallbacks
    def cmd_encoding(self, user, channel, msg):
        """.encoding || .encoding || Lists the shows currently encoding."""
        if not LOCAL:
            return

        # finds and returns a count of instances of a process
        cmd = 'WMIC process where Caption="x320.exe" get Commandline /Format:csv'.split(" ")

        # execute the command
        output = yield utils.getProcessOutput(getPath(cmd[0]), args=cmd[1:], env=os.environ, errortoo=True)

        # check if anything is encoding, otherwise tell that nothing is encoding
        if "No Instance(s) Available" in output:
            self.msg(channel, 'Nothing is encoding right now')
        else:
            EnumWindows(EnumWindowsProc(self.foreach_window), 0)
            
    @defer.inlineCallbacks
    def foreach_window(self, hwnd, lParam):
        # make sure it's a visible window
        if IsWindowVisible(hwnd):
            # get the length of the title, create a buffer then fetch the text to the buffer
            length = GetWindowTextLength(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            GetWindowText(hwnd, buff, length + 1)
            
            # if the title contains an illegal character it will throw an error (like the (tm) in Skype(tm)). 
            # doesn't matter in out case tho, we only need the title of the x264 window which contain no such chars 
            try:
                windowtitle = buff.value;
                if "x264" in windowtitle:
                    # get the proccessid from the windowhandle
                    processID = ctypes.c_int()
                    threadID = GetWindowThreadProcessId(hwnd,ctypes.byref(processID))
                    # we use WMIC to fetch the commandline of the process. It's a commandline interface for WMI
                    # a sort of query language to fetch various OS stuff. 
                    # more info: http://technet.microsoft.com/en-us/library/bb742610.aspx
                    cmd = 'WMIC process where processid='+str(processID.value)+' get Commandline /Format:csv'.split(" ")
                    
                    # execute the command
                    output = yield utils.getProcessOutput(getPath(cmd[0]), args=cmd[1:], env=os.environ, errortoo=True)
                    
                    file = output.strip().split(" ")[-1];
                    split = file.split("\\");
                    rg = re.compile('(\d+(v\d+)?)',re.IGNORECASE|re.DOTALL);
                    m = rg.search(split[0]);
                    if m:
                        split[0] = re.sub(m.group(1), "", split[0]);
                        split[0] += " "+m.group(1);
                    message = split[0]+" part "+split[-1][:split[-1].find(".")]+": "+windowtitle;
                    self.msg("#commie-staff", message)
            except:
                # something went wrong, write in xchat console
                log(__module_name__+": Unexpected error:"+ sys.exc_info()[0])
    
    @admin
    @defer.inlineCallbacks
    def cmd_hulu(self, user, channel, msg):
        """.hulu [Hulu ID] [show name] || .hulu 401199 Accel World || Rips subs from hulu and updates the chart"""
        hid = msg[0]
        show = self.factory.resolve(" ".join(msg[1:]), channel)
        if show is None:
            return
        ep = int(show["current_ep"]) + 1
        fname = "%s/[Hulu] %s %d.ass" % (self.factory.ass_destdir, show["series"], ep)
        nfname = "%s/[Hulu] %s %d.ass" % (self.factory.observe_dir, show["series"], ep)
        data = yield fetchPage("http://commie.fugiman.com/hulu/%s/" % hid)
        with open(fname, "w") as f:
            f.write(data)
        os.rename(fname, nfname)
        data = {"id":show["id"],"method":"position_status","position":"translator","value":"1"}
        self.factory.load("show","update", data=data)
        self.msg(channel, "Script for %s %d is ripped and translator marked as done" % (show["series"], ep))
        
    
    @admin
    def cmd_rip(self, user, channel, msg):
        """.rip [file name] || .rip [HorribleSubs] Natsuyuki Rendezvous - 11 [720p].mkv || Downloads the file and rips the subs"""
        fname = " ".join(msg)
        self.rips[fname.replace(" ","_")] = self.rips[fname] = {"name":fname,"failed":[],"responses":{},"requestor":user,"begun":False,"transfering":False}
        for b in self.factory.bots:
            self.msg(b,"XDCC SEARCH %s" % fname)
        reactor.callLater(15, self.rip_begin, fname)
    
    # Ripper stuff
    def noticed(self, user, channel, msg):
        user = user.split("!", 1)[0].lower()
        if user in self.factory.bots:
            match = re.match('Searching for "(.*?)"...',msg)
            if match is not None:
                self.bots[user] = match.group(1)
            elif user in self.bots:
                fname = self.bots[user]
                match = re.match(r".*?Pack #(\d+) matches", msg)
                if fname not in self.rips:
                    pass # Just chill out for a bit
                elif msg == "Sorry, nothing was found, try a XDCC LIST":
                    if fname.count(" "):
                        self.msg(user, "XDCC SEARCH %s" % fname.replace(" ","_")) # Try again with underscores
                    else:
                        self.rips[fname]["failed"].append(user)
                        self.notice(self.rips[fname]["requestor"], "Bot %s failed to find the file" % user)
                elif match is not None:
                    self.rips[fname]["responses"][user] = match.group(1)
                    if len(self.rips[fname]["responses"].keys()) == (len(self.factory.bots) - len(self.rips[fname]["failed"])):
                        self.rip_begin(fname)
                    self.bots[user] = False
                else:
                    self.notice(self.rips[fname]["requestor"], "Bot %s gave an unparsable response" % user)
    
    def dccDoSend(self, user, address, port, filename, size, data):
        user = user.split("!", 1)[0]
        if (user.lower() in self.factory.admins or user.lower() in self.factory.bots) and filename in self.rips:
            factory = ServrheDCCFactory(filename.replace(" ","_"), size, (user, data), self.factory.dcc_destdir)
            factory.set_overwrite(True)
            factory.master = self
            reactor.connectTCP(address, port, factory)
            self.rips[filename]["transfering"] = True
    
    def rip_begin(self, fname):
        if self.rips[fname]["begun"]:
            return # Already been here
        self.rips[fname]["begun"] = True
        bot = False
        for b in self.factory.bots:
            if b in self.rips[fname]["responses"]:
                bot = b
                break
        if bot:
            self.notice(self.rips[fname]["requestor"], "Loading #%s from %s" % (self.rips[fname]["responses"][bot], bot))
            self.msg(bot, "XDCC SEND %s" % self.rips[fname]["responses"][bot])
            reactor.callLater(10, self.rip_check, fname)
        else:
            self.notice(self.rips[fname]["requestor"], "All bots failed to respond in a timely manner")
            fname = self.rips[fname]["name"]
            del self.rips[fname]
            if fname != fname.replace(" ","_"):
                del self.rips[fname.replace(" ","_")]
    
    def rip_check(self, fname):
        if not self.rips[fname]["transfering"]:
            self.notice(self.rips[fname]["requestor"], "DCC transfer failed to start in a timely manner")
            fname = self.rips[fname]["name"]
            del self.rips[fname]
            if fname != fname.replace(" ","_"):
                del self.rips[fname.replace(" ","_")]
    
    @defer.inlineCallbacks
    def rip_success(self, fname):
        name, ext = os.path.splitext(fname)
        full = "%s/%s" % (self.factory.dcc_destdir, fname)
        dest = "%s/%s.ass" % (self.factory.ass_destdir, name)
        final = "%s/%s.ass" % (self.factory.observe_dir, name)
        if ext != ".mkv":
            self.notice(self.rips[fname]["requestor"], "%s isn't an MKV" % fname)
        else:
            exitCode = yield utils.getProcessValue(getPath("mkvextract"), args=["tracks",full,"3:%s"%dest], env=os.environ)
            if exitCode == 0:
                self.notice(self.rips[fname]["requestor"], "Subtitles for %s extracted successfully." % fname)
                if LOCAL:
                    os.rename(dest, final)
                else:
                    yield utils.getProcessValue(getPath("curl"), args=["--globoff","-T",dest,self.factory.ftp_location], env=os.environ)
            else:
                self.notice(self.rips[fname]["requestor"], "Subtitles for %s failed to extract." % fname)
        fname = self.rips[fname]["name"]
        del self.rips[fname]
        if fname != fname.replace(" ","_"):
            del self.rips[fname.replace(" ","_")]
        os.remove(full)
    
    def rip_fail(self, fname):
        self.notice(self.rips[fname]["requestor"], "DCC transfer for %s failed or aborted." % fname)
        fname = self.rips[fname]["name"]
        del self.rips[fname]
        del self.rips[fname.replace(" ","_")]

    @owner
    @defer.inlineCallbacks
    def cmd_save(self, user, channel, msg):
        """.save || .save || Saves the config and uploads it to the cloud"""
        self.factory.save_config()
        config = "{}"
        with open("servrhe.json") as f:
            config = f.read()
        data = urllib.urlencode({
            "key": self.factory.key,
            "config": config
        })
        headers = {'Content-Type': ['application/x-www-form-urlencoded']}
        msg = yield fetchPage(self.factory.save_url, data, headers)
        self.notice(user, msg)

    @owner
    @defer.inlineCallbacks
    def cmd_load(self, user, channel, msg):
        """.load || .load || Downloads config from the cloud and loads it"""
        data = urllib.urlencode({
            "key": self.factory.key,
            "type": "config"
        })
        headers = {'Content-Type': ['application/x-www-form-urlencoded']}
        config = yield fetchPage(self.factory.load_url, data, headers)
        with open("servrhe.json","w") as f:
            f.write(config)
        self.factory.load_config()
        self.notice(user, "Config reloaded")

    @owner
    @defer.inlineCallbacks
    def cmd_update(self, user, channel, msg):
        """.update || .update || Downloads source code from cloud and reboots bot"""
        data = urllib.urlencode({
            "key": self.factory.key,
            "type": "bot"
        })
        headers = {'Content-Type': ['application/x-www-form-urlencoded']}
        config = yield fetchPage(self.factory.load_url, data, headers)
        with open("servrhe.py","wb") as f:
            f.write(config)
        # Restart the bot
        self.quit("Updating, be back shortly")
        args = sys.argv[:]
        args.insert(0, sys.executable)
        if sys.platform == 'win32':
            args = ['"%s"' % arg for arg in args]
        os.execv(sys.executable, args)
        reactor.stop()

    @owner
    def cmd_tail(self, user, channel, msg):
        """.tail [entries] || .tail 10 || Lists last X entries from command log"""
        length = int(msg[0]) if msg else 5
        for m in logs[-1*length:]:
            self.notice(user, m)

    @owner
    def cmd_ls(self, user, channel, msg):
        """.ls [directory] || .ls /home/ubuntu/porn || Lists contents of directory"""
        path = unicode(msg[0]) if msg else u"."
        self.notice(user, ", ".join([s.encode("utf-8") for s in os.listdir(path)]))

    @owner
    def cmd_passthru(self, user, channel, msg):
        """.passthru [command] [args] ... || .passthru curl --globoff -T ftp://idk.com/ || Runs a command and returns the output"""
        msg = [unicode(c).encode("utf-8") for c in msg]
        try:
            d = utils.getProcessOutput(getPath(msg[0]), args=msg[1:], env=os.environ, errortoo=True)
            d.addCallback(self.notice, user)
            d.addErrback(self.notice, user, "An error occurred")
        except Exception, e:
            self.notice(user, str(e))

class ServrheFactory(protocol.ReconnectingClientFactory):
    # Protocol config
    protocol = Servrhe
    maxDelay = 5 * 60 # 5 minutes
    # Bot config
    password = ""
    channels = ["#commie-subs","#commie-staff"]
    admins = ["rhexcelion","fugiman"]
    bots = ["arutha","cerebrate","[h-subs]rei","vesperia"]
    releases = []
    # Ripper config
    dcc_destdir = "C:/"
    ass_destdir = "C:/"
    ftp_location = ""
    # Showtime config
    key = ""
    base = "http://commie.milkteafuzz.com/st"
    positions = ["translator","editor","typesetter","timer","encoding"]
    shows = False
    # Filesystem watching
    observer = None
    observe_dir = "C:/"
    protocols = []
    highlights = {}
    observer_running = False
    # Remote operation
    save_url = "http://fugiman.com/commie/save.php"
    load_url = "http://fugiman.com/commie/load.php"
    # Topic bullshit. <3 rhe
    topic = ["☭ Commie Subs ☭",20,20.56]
    
    def __init__(self):
        self.shows = {}
        self.load_config()
        reactor.addSystemEventTrigger("before", "shutdown", self.shutdown)
        t = task.LoopingCall(self.refresh_shows)
        t.start(5*60) # 5 minutes
        event_handler = ServrheObserver(self, reactor, self.observe_dir)
        self.observer = Observer()
        self.observer.schedule(event_handler, path=self.observe_dir)
        self.observer.start()
        self.observer_running = True
    
    def file_change(self, file, size):
        message = "\x02\x034File Added\x0F - {} ({})".format(file, size)
        matches = []
        for key, people in self.highlights.items():
            if fnmatch.fnmatch(file, key):
                matches.extend(people)
        if matches:
            message = "{} < {}".format(message, " ".join(matches))
        self.broadcast(message)
    
    def broadcast(self, message):
        if self.protocols:
            self.protocols[0].msg("#commie-staff", message)
    
    def load(self, *params, **kwargs):
        url = "/".join([self.base]+[str(x) for x in params])
        url = urllib.quote(url.encode("utf-8","ignore"),"/:")
        headers = {}
        data = ""
        if "data" in kwargs:
            d = kwargs["data"]
            d["key"] = self.key
            data = json.dumps(d)
            headers["Content-Type"] = ["application/json"]
        d = fetchPage(url, data, headers)
        d.addCallback(json.loads)
        return d
    
    @defer.inlineCallbacks
    def refresh_shows(self):
        data = yield self.load("shows")
        if "status" in data and not data["status"]:
            self.broadcast(data["message"])
            return
        data = data["results"]
        for show in data:
            self.shows[show["id"]] = show
    
    def resolve(self, show, channel):
        matches = []
        for s in self.shows.itervalues():
            if s["series"].lower() == show.lower():
                return s
            if s["series"].lower().count(show.lower()):
                matches.append(s)
        if len(matches) > 1:
            self.protocols[0].msg(channel, "Show name not specific, found: %s" % ", ".join([s["series"] for s in matches]))
            return None
        elif not matches:
            self.protocols[0].msg(channel, "Show name not found.")
            return None
        return matches[0]
    
    @defer.inlineCallbacks
    def update_topic(self):
        shows = yield self.load("shows","current_episodes")
        shows = [(s["abbr"], s["current_ep"], s["last_release"]) for s in shows["results"]]
        shows.sort(key=lambda x: x[2], reverse=True)
        shows = ", ".join(["{} {:d}".format(s[0],s[1]) for s in shows[:self.topic[1]]])
        topic = " || ".join([self.topic[0], shows, "Mahoyo progress: {:0.2f}%".format(self.topic[2])] + self.topic[3:])
        topic = unicode(topic).encode("utf-8")
        self.protocols[0].topic("#commie-subs", topic)

    def load_config(self):
        try:
            with open("servrhe.json","r") as f:
                config = json.loads(f.read())
                self.password = str(config["password"]) if "password" in config else self.password
                self.channels = [str(c) for c in config["channels"]] if "channels" in config else self.channels
                self.admins = [str(a) for a in config["admins"]] if "admins" in config else self.admins
                self.bots = [str(b) for b in config["bots"]] if "bots" in config else self.bots
                self.releases = [str(b) for b in config["releases"]] if "releases" in config else self.releases
                self.dcc_destdir = str(config["dcc_destdir"]) if "dcc_destdir" in config else self.dcc_destdir
                self.ass_destdir = str(config["ass_destdir"]) if "ass_destdir" in config else self.ass_destdir
                self.ftp_location = str(config["ftp_location"]) if "ftp_location" in config else self.ass_destdir
                self.key = str(config["key"]) if "key" in config else self.key
                self.base = str(config["base"]) if "base" in config else self.base
                self.positions = [str(b) for b in config["positions"]] if "positions" in config else self.positions
                self.observe_dir = str(config["observe_dir"]) if "observe_dir" in config else self.observe_dir
                self.highlights = config["highlights"] if "highlights" in config else self.highlights
                self.topic = config["topic"] if "topic" in config else self.topic
        except IOError:
            pass # File doesn't exist, use defaults
        self.admins.sort()

    def save_config(self):
        config = {}
        config["password"] = self.password
        config["channels"] = self.channels
        config["admins"] = self.admins
        config["bots"] = self.bots
        config["releases"] = self.releases
        config["dcc_destdir"] = self.dcc_destdir
        config["ass_destdir"] = self.ass_destdir
        config["ftp_location"] = self.ftp_location
        config["key"] = self.key
        config["base"] = self.base
        config["positions"] = self.positions
        config["observe_dir"] = self.observe_dir
        config["highlights"] = self.highlights
        config["topic"] = self.topic
        data = json.dumps(config)
        with open("servrhe.json","w") as f:
            f.write(data)

    def shutdown(self):
        self.save_config()
        if self.observer_running:
            self.observer.stop()
    
if __name__ == "__main__":
    factory = ServrheFactory()
    reactor.connectTCP("irc.rizon.net", 6667, factory)
    reactor.run()