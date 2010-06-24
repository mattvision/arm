#!/usr/bin/env python
# logPanel.py -- Resources related to Tor event monitoring.
# Released under the GPL v3 (http://www.gnu.org/licenses/gpl.html)

import time
import curses
from curses.ascii import isprint
from TorCtl import TorCtl

from util import log, panel, sysTools, uiTools

PRE_POPULATE_LOG = True               # attempts to retrieve events from log file if available

# truncates to the last X log lines (needed to start in a decent time if the log's big)
PRE_POPULATE_MIN_LIMIT = 1000             # limit in case of verbose logging
PRE_POPULATE_MAX_LIMIT = 5000             # limit for NOTICE - ERR (since most lines are skipped)
MAX_LOG_ENTRIES = 1000                # size of log buffer (max number of entries)
RUNLEVEL_EVENT_COLOR = {"DEBUG": "magenta", "INFO": "blue", "NOTICE": "green", "WARN": "yellow", "ERR": "red"}

TOR_EVENT_TYPES = {
  "d": "DEBUG",   "a": "ADDRMAP",       "l": "NEWDESC",       "v": "AUTHDIR_NEWDESCS",
  "i": "INFO",    "b": "BW",            "m": "NS",            "x": "STATUS_GENERAL",
  "n": "NOTICE",  "c": "CIRC",          "o": "ORCONN",        "y": "STATUS_CLIENT",
  "w": "WARN",    "f": "DESCCHANGED",   "s": "STREAM",        "z": "STATUS_SERVER",
  "e": "ERR",     "g": "GUARD",         "t": "STREAM_BW",
                  "k": "NEWCONSENSUS",  "u": "CLIENTS_SEEN"}

EVENT_LISTING = """        d DEBUG     a ADDRMAP         l NEWDESC         v AUTHDIR_NEWDESCS
        i INFO      b BW              m NS              x STATUS_GENERAL
        n NOTICE    c CIRC            o ORCONN          y STATUS_CLIENT
        w WARN      f DESCCHANGED     s STREAM          z STATUS_SERVER
        e ERR       g GUARD           t STREAM_BW       A All Events
                    k NEWCONSENSUS    u CLIENTS_SEEN    X No Events
          DINWE runlevel and higher severity            C TorCtl Events
          12345 arm runlevel and higher severity        U Unknown Events"""

TOR_CTL_CLOSE_MSG = "Tor closed control connection. Exiting event thread."

def expandEvents(eventAbbr):
  """
  Expands event abbreviations to their full names. Beside mappings privided in
  TOR_EVENT_TYPES this recognizes the following special events and aliases:
  C - TORCTL runlevel events
  U - UKNOWN events
  A - all events
  X - no events
  DINWE - runlevel and higher
  12345 - arm runlevel and higher (ARM_DEBUG - ARM_ERR)
  Raises ValueError with invalid input if any part isn't recognized.
  
  Examples:
  "inUt" -> ["INFO", "NOTICE", "UNKNOWN", "STREAM_BW"]
  "N4" -> ["NOTICE", "WARN", "ERR", "ARM_WARN", "ARM_ERR"]
  "cfX" -> []
  """
  
  expandedEvents = set()
  invalidFlags = ""
  for flag in eventAbbr:
    if flag == "A":
      expandedEvents = set(TOR_EVENT_TYPES.values() + ["ARM_DEBUG", "ARM_INFO", "ARM_NOTICE", "ARM_WARN", "ARM_ERR"])
      break
    elif flag == "X":
      expandedEvents = set()
      break
    elif flag == "C": expandedEvents.add("TORCTL")
    elif flag == "U": expandedEvents.add("UNKNOWN")
    elif flag == "D": expandedEvents = expandedEvents.union(set(["DEBUG", "INFO", "NOTICE", "WARN", "ERR"]))
    elif flag == "I": expandedEvents = expandedEvents.union(set(["INFO", "NOTICE", "WARN", "ERR"]))
    elif flag == "N": expandedEvents = expandedEvents.union(set(["NOTICE", "WARN", "ERR"]))
    elif flag == "W": expandedEvents = expandedEvents.union(set(["WARN", "ERR"]))
    elif flag == "E": expandedEvents.add("ERR")
    elif flag == "1": expandedEvents = expandedEvents.union(set(["ARM_DEBUG", "ARM_INFO", "ARM_NOTICE", "ARM_WARN", "ARM_ERR"]))
    elif flag == "2": expandedEvents = expandedEvents.union(set(["ARM_INFO", "ARM_NOTICE", "ARM_WARN", "ARM_ERR"]))
    elif flag == "3": expandedEvents = expandedEvents.union(set(["ARM_NOTICE", "ARM_WARN", "ARM_ERR"]))
    elif flag == "4": expandedEvents = expandedEvents.union(set(["ARM_WARN", "ARM_ERR"]))
    elif flag == "5": expandedEvents.add("ARM_ERR")
    elif flag in TOR_EVENT_TYPES:
      expandedEvents.add(TOR_EVENT_TYPES[flag])
    else:
      invalidFlags += flag
  
  if invalidFlags: raise ValueError(invalidFlags)
  else: return expandedEvents

class LogMonitor(TorCtl.PostEventListener, panel.Panel):
  """
  Tor event listener, noting messages, the time, and their type in a panel.
  """
  
  def __init__(self, stdscr, conn, loggedEvents):
    TorCtl.PostEventListener.__init__(self)
    panel.Panel.__init__(self, stdscr, "log", 0)
    self.scroll = 0
    self.msgLog = []                      # tuples of (logText, color)
    self.isPaused = False
    self.pauseBuffer = []                 # location where messages are buffered if paused
    self.loggedEvents = loggedEvents      # events we're listening to
    self.lastHeartbeat = time.time()      # time of last event
    self.regexFilter = None               # filter for presented log events (no filtering if None)
    self.eventTimeOverwrite = None        # replaces time for further events with this (uses time it occures if None)
    self.controlPortClosed = False        # flag set if TorCtl provided notice that control port is closed
    
    # prevents attempts to redraw while processing batch of events
    previousPauseState = self.isPaused
    self.setPaused(True)
    log.addListeners([log.DEBUG, log.INFO, log.NOTICE, log.WARN, log.ERR], self.arm_event_wrapper, True)
    self.setPaused(previousPauseState)
    
    # attempts to process events from log file
    if PRE_POPULATE_LOG:
      previousPauseState = self.isPaused
      
      try:
        logFileLoc = None
        loggingLocations = conn.get_option("Log")
        
        for entry in loggingLocations:
          entryComp = entry[1].split()
          if entryComp[1] == "file":
            logFileLoc = entryComp[2]
            break
        
        if logFileLoc:
          # prevents attempts to redraw while processing batch of events
          self.setPaused(True)
          
          # trims log to last entries to deal with logs when they're in the GB or TB range
          # throws IOError if tail fails (falls to the catch-all later)
          # TODO: now that this is using sysTools figure out if we can do away with the catch-all...
          limit = PRE_POPULATE_MIN_LIMIT if ("DEBUG" in self.loggedEvents or "INFO" in self.loggedEvents) else PRE_POPULATE_MAX_LIMIT
          
          # truncates to entries for this tor instance
          lines = sysTools.call("tail -n %i %s" % (limit, logFileLoc))
          instanceStart = 0
          for i in range(len(lines) - 1, -1, -1):
            if "opening log file" in lines[i]:
              instanceStart = i
              break
          
          for line in lines[instanceStart:]:
            lineComp = line.split()
            eventType = lineComp[3][1:-1].upper()
            
            if eventType in self.loggedEvents:
              timeComp = lineComp[2][:lineComp[2].find(".")].split(":")
              self.eventTimeOverwrite = (0, 0, 0, int(timeComp[0]), int(timeComp[1]), int(timeComp[2]))
              self.listen(TorCtl.LogEvent(eventType, " ".join(lineComp[4:])))
      except Exception: pass # disreguard any issues that might arise
      finally:
        self.setPaused(previousPauseState)
        self.eventTimeOverwrite = None
  
  def handleKey(self, key):
    # scroll movement
    if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
      pageHeight, shift = self.getPreferredSize()[0] - 1, 0
      
      # location offset
      if key == curses.KEY_UP: shift = -1
      elif key == curses.KEY_DOWN: shift = 1
      elif key == curses.KEY_PPAGE: shift = -pageHeight
      elif key == curses.KEY_NPAGE: shift = pageHeight
      
      # restricts to valid bounds and applies
      maxLoc = self.getLogDisplayLength() - pageHeight
      self.scroll = max(0, min(self.scroll + shift, maxLoc))
  
  # Listens for all event types and redirects to registerEvent
  def circ_status_event(self, event):
    if "CIRC" in self.loggedEvents:
      optionalParams = ""
      if event.purpose: optionalParams += " PURPOSE: %s" % event.purpose
      if event.reason: optionalParams += " REASON: %s" % event.reason
      if event.remote_reason: optionalParams += " REMOTE_REASON: %s" % event.remote_reason
      self.registerEvent("CIRC", "ID: %-3s STATUS: %-10s PATH: %s%s" % (event.circ_id, event.status, ", ".join(event.path), optionalParams), "yellow")
  
  def stream_status_event(self, event):
    # TODO: not sure how to stimulate event - needs sanity check
    try:
      self.registerEvent("STREAM", "ID: %s STATUS: %s CIRC_ID: %s TARGET: %s:%s REASON: %s REMOTE_REASON: %s SOURCE: %s SOURCE_ADDR: %s PURPOSE: %s" % (event.strm_id, event.status, event.circ_id, event.target_host, event.target_port, event.reason, event.remote_reason, event.source, event.source_addr, event.purpose), "white")
    except TypeError:
      self.registerEvent("STREAM", "DEBUG -> ID: %s STATUS: %s CIRC_ID: %s TARGET: %s:%s REASON: %s REMOTE_REASON: %s SOURCE: %s SOURCE_ADDR: %s PURPOSE: %s" % (type(event.strm_id), type(event.status), type(event.circ_id), type(event.target_host), type(event.target_port), type(event.reason), type(event.remote_reason), type(event.source), type(event.source_addr), type(event.purpose)), "white")
  
  def or_conn_status_event(self, event):
    optionalParams = ""
    if event.age: optionalParams += " AGE: %-3s" % event.age
    if event.read_bytes: optionalParams += " READ: %-4i" % event.read_bytes
    if event.wrote_bytes: optionalParams += " WRITTEN: %-4i" % event.wrote_bytes
    if event.reason: optionalParams += " REASON: %-6s" % event.reason
    if event.ncircs: optionalParams += " NCIRCS: %i" % event.ncircs
    self.registerEvent("ORCONN", "STATUS: %-10s ENDPOINT: %-20s%s" % (event.status, event.endpoint, optionalParams), "white")
  
  def stream_bw_event(self, event):
    # TODO: not sure how to stimulate event - needs sanity check
    try:
      self.registerEvent("STREAM_BW", "ID: %s READ: %i WRITTEN: %i" % (event.strm_id, event.bytes_read, event.bytes_written), "white")
    except TypeError:
      self.registerEvent("STREAM_BW", "DEBUG -> ID: %s READ: %s WRITTEN: %s" % (type(event.strm_id), type(event.bytes_read), type(event.bytes_written)), "white")
  
  def bandwidth_event(self, event):
    self.lastHeartbeat = time.time() # ensures heartbeat at least once a second
    if "BW" in self.loggedEvents: self.registerEvent("BW", "READ: %i, WRITTEN: %i" % (event.read, event.written), "cyan")
  
  def msg_event(self, event):
    self.registerEvent(event.level, event.msg, RUNLEVEL_EVENT_COLOR[event.level])
  
  def new_desc_event(self, event):
    if "NEWDESC" in self.loggedEvents:
      idlistStr = [str(item) for item in event.idlist]
      self.registerEvent("NEWDESC", ", ".join(idlistStr), "white")
  
  def address_mapped_event(self, event):
    self.registerEvent("ADDRMAP", "%s, %s -> %s" % (event.when, event.from_addr, event.to_addr), "white")
  
  def ns_event(self, event):
    # NetworkStatus params: nickname, idhash, orhash, ip, orport (int), dirport (int), flags, idhex, bandwidth, updated (datetime)
    msg = ""
    for ns in event.nslist:
      msg += ", %s (%s:%i)" % (ns.nickname, ns.ip, ns.orport)
    if len(msg) > 1: msg = msg[2:]
    self.registerEvent("NS", "Listed (%i): %s" % (len(event.nslist), msg), "blue")
  
  def new_consensus_event(self, event):
    if "NEWCONSENSUS" in self.loggedEvents:
      msg = ""
      for ns in event.nslist:
        msg += ", %s (%s:%i)" % (ns.nickname, ns.ip, ns.orport)
      self.registerEvent("NEWCONSENSUS", "Listed (%i): %s" % (len(event.nslist), msg), "magenta")
  
  def unknown_event(self, event):
    if "UNKNOWN" in self.loggedEvents: self.registerEvent("UNKNOWN", event.event_string, "red")
  
  def arm_event_wrapper(self, level, msg, eventTime):
    # temporary adaptor hack to use the new logging functions until I'm sure they'll work
    # TODO: insert into log according to the event's timestamp (harder part
    # here will be interpreting tor's event timestamps...)
    self.monitor_event(level, msg)
  
  def monitor_event(self, level, msg):
    # events provided by the arm monitor
    if "ARM_" + level in self.loggedEvents: self.registerEvent("ARM-%s" % level, msg, RUNLEVEL_EVENT_COLOR[level])
  
  def tor_ctl_event(self, level, msg):
    # events provided by TorCtl
    if "TORCTL" in self.loggedEvents: self.registerEvent("TORCTL-%s" % level, msg, RUNLEVEL_EVENT_COLOR[level])
  
  def write(self, msg):
    """
    Tracks TorCtl events. Ugly hack since TorCtl/TorUtil.py expects a file.
    """
    
    timestampStart = msg.find("[")
    timestampEnd = msg.find("]")
    
    level = msg[:timestampStart]
    msg = msg[timestampEnd + 2:].strip()
    
    if TOR_CTL_CLOSE_MSG in msg:
      # TorCtl providing notice that control port is closed
      self.controlPortClosed = True
      log.log(log.NOTICE, "Tor control port closed")
    self.tor_ctl_event(level, msg)
  
  def flush(self): pass
  
  def registerEvent(self, type, msg, color):
    """
    Notes event and redraws log. If paused it's held in a temporary buffer. If 
    msg is a list then this is expanded to multiple lines.
    """
    
    if not type.startswith("ARM"): self.lastHeartbeat = time.time()
    eventTime = self.eventTimeOverwrite if self.eventTimeOverwrite else time.localtime()
    toAdd = []
    
    # wraps if a single line message
    if isinstance(msg, str): msg = [msg]
    
    firstLine = True
    for msgLine in msg:
      # strips control characters to avoid screwing up the terminal
      msgLine = "".join([char for char in msgLine if isprint(char)])
      
      header = "%02i:%02i:%02i %s" % (eventTime[3], eventTime[4], eventTime[5], "[%s]" % type) if firstLine else ""
      toAdd.append("%s %s" % (header, msgLine))
      firstLine = False
    
    toAdd.reverse()
    if self.isPaused:
      for msgLine in toAdd: self.pauseBuffer.insert(0, (msgLine, color))
      if len(self.pauseBuffer) > MAX_LOG_ENTRIES: del self.pauseBuffer[MAX_LOG_ENTRIES:]
    else:
      for msgLine in toAdd: self.msgLog.insert(0, (msgLine, color))
      if len(self.msgLog) > MAX_LOG_ENTRIES: del self.msgLog[MAX_LOG_ENTRIES:]
      self.redraw(True)
  
  def draw(self, subwindow, width, height):
    """
    Redraws message log. Entries stretch to use available space and may
    contain up to two lines. Starts with newest entries.
    """
    
    isScrollBarVisible = self.getLogDisplayLength() > height - 1
    xOffset = 3 if isScrollBarVisible else 0 # content offset for scroll bar
    
    # draws label - uses ellipsis if too long, for instance:
    # Events (DEBUG, INFO, NOTICE, WARN...):
    eventsLabel = "Events"
    
    # separates tor and arm runlevels (might be able to show as range)
    eventsList = list(self.loggedEvents)
    torRunlevelLabel = ", ".join(parseRunlevelRanges(eventsList, ""))
    armRunlevelLabel = ", ".join(parseRunlevelRanges(eventsList, "ARM_"))
    
    if armRunlevelLabel: eventsList = ["ARM " + armRunlevelLabel] + eventsList
    if torRunlevelLabel: eventsList = [torRunlevelLabel] + eventsList
    
    eventsListing = ", ".join(eventsList)
    filterLabel = "" if not self.regexFilter else " - filter: %s" % self.regexFilter.pattern
    
    firstLabelLen = eventsListing.find(", ")
    if firstLabelLen == -1: firstLabelLen = len(eventsListing)
    else: firstLabelLen += 3
    
    if width > 10 + firstLabelLen:
      eventsLabel += " ("
      
      if len(eventsListing) > width - 11:
        labelBreak = eventsListing[:width - 12].rfind(", ")
        eventsLabel += "%s..." % eventsListing[:labelBreak]
      elif len(eventsListing) + len(filterLabel) > width - 11:
        eventsLabel += eventsListing
      else: eventsLabel += eventsListing + filterLabel
      eventsLabel += ")"
    eventsLabel += ":"
    
    self.addstr(0, 0, eventsLabel, curses.A_STANDOUT)
    
    # log entries
    maxLoc = self.getLogDisplayLength() - height + 1
    self.scroll = max(0, min(self.scroll, maxLoc))
    lineCount = 1 - self.scroll
    
    for (line, color) in self.msgLog:
      if self.regexFilter and not self.regexFilter.search(line):
        continue  # filter doesn't match log message - skip
      
      # splits over too lines if too long
      if len(line) < width:
        if lineCount >= 1: self.addstr(lineCount, xOffset, line, uiTools.getColor(color))
        lineCount += 1
      else:
        (line1, line2) = splitLine(line, width - xOffset)
        if lineCount >= 1: self.addstr(lineCount, xOffset, line1, uiTools.getColor(color))
        if lineCount >= 0: self.addstr(lineCount + 1, xOffset, line2, uiTools.getColor(color))
        lineCount += 2
      
      if lineCount >= height: break # further log messages wouldn't fit
    
    if isScrollBarVisible: self.addScrollBar(self.scroll, self.scroll + height - 1, self.getLogDisplayLength(), 1)
  
  def getLogDisplayLength(self):
    """
    Provides the number of lines the log would currently occupy.
    """
    
    logLength = len(self.msgLog)
    
    # takes into account filtered and wrapped messages
    for (line, color) in self.msgLog:
      if self.regexFilter and not self.regexFilter.search(line): logLength -= 1
      elif len(line) >= self.getPreferredSize()[1]: logLength += 1
    
    return logLength
  
  def setPaused(self, isPause):
    """
    If true, prevents message log from being updated with new events.
    """
    
    if isPause == self.isPaused: return
    
    self.isPaused = isPause
    if self.isPaused: self.pauseBuffer = []
    else:
      self.msgLog = (self.pauseBuffer + self.msgLog)[:MAX_LOG_ENTRIES]
      if self.win: self.redraw(True) # hack to avoid redrawing during init
  
  def getHeartbeat(self):
    """
    Provides the number of seconds since the last registered event (this always
    listens to BW events so should be less than a second if relay's still
    responsive).
    """
    
    return time.time() - self.lastHeartbeat

def parseRunlevelRanges(eventsList, searchPrefix):
  """
  This parses a list of events to provide an ordered list of runlevels, 
  condensed if three or more are in a contiguous range. This removes parsed 
  runlevels from the eventsList. For instance:
  
  eventsList = ["BW", "ARM_WARN", "ERR", "ARM_ERR", "ARM_DEBUG", "ARM_NOTICE"]
  searchPrefix = "ARM_"
  
  results in:
  eventsList = ["BW", "ERR"]
  return value is ["DEBUG", "NOTICE - ERR"]
  
  """
  
  # blank ending runlevel forces the break condition to be reached at the end
  runlevels = ["DEBUG", "INFO", "NOTICE", "WARN", "ERR", ""]
  runlevelLabels = []
  start, end = "", ""
  rangeLength = 0
  
  for level in runlevels:
    if searchPrefix + level in eventsList:
      eventsList.remove(searchPrefix + level)
      
      if start:
        end = level
        rangeLength += 1
      else:
        start = level
        rangeLength = 1
    elif rangeLength > 0:
      # reached a break in the runlevels
      if rangeLength == 1: runlevelLabels += [start]
      elif rangeLength == 2: runlevelLabels += [start, end]
      else: runlevelLabels += ["%s - %s" % (start, end)]
      
      start, end = "", ""
      rangeLength = 0
  
  return runlevelLabels

def splitLine(message, x):
  """
  Divides message into two lines, attempting to do it on a wordbreak.
  """
  
  lastWordbreak = message[:x].rfind(" ")
  if x - lastWordbreak < 10:
    line1 = message[:lastWordbreak]
    line2 = "  %s" % message[lastWordbreak:].strip()
  else:
    # over ten characters until the last word - dividing
    line1 = "%s-" % message[:x - 2]
    line2 = "  %s" % message[x - 2:].strip()
  
  # ends line with ellipsis if too long
  if len(line2) > x:
    lastWordbreak = line2[:x - 4].rfind(" ")
    
    # doesn't use wordbreak if it's a long word or the whole line is one 
    # word (picking up on two space indent to have index 1)
    if x - lastWordbreak > 10 or lastWordbreak == 1: lastWordbreak = x - 4
    line2 = "%s..." % line2[:lastWordbreak]
  
  return (line1, line2)

