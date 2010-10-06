"""
Helper for working with an active tor process. This both provides a wrapper for
accessing TorCtl and notifications of state changes to subscribers. To quickly
fetch a TorCtl instance to experiment with use the following:

>>> import util.torTools
>>> conn = util.torTools.connect()
>>> conn.get_info("version")["version"]
'0.2.1.24'
"""

import os
import time
import socket
import thread
import threading

from TorCtl import TorCtl, TorUtil

from util import log, sysTools

# enums for tor's controller state:
# TOR_INIT - attached to a new controller or restart/sighup signal received
# TOR_CLOSED - control port closed
TOR_INIT, TOR_CLOSED = range(1, 3)

# message logged by default when a controller can't set an event type
DEFAULT_FAILED_EVENT_MSG = "Unsupported event type: %s"

# TODO: check version when reattaching to controller and if version changes, flush?
# Skips attempting to set events we've failed to set before. This avoids
# logging duplicate warnings but can be problematic if controllers belonging
# to multiple versions of tor are attached, making this unreflective of the
# controller's capabilites. However, this is a pretty bizarre edge case.
DROP_FAILED_EVENTS = True
FAILED_EVENTS = set()

CONTROLLER = None # singleton Controller instance

# Valid keys for the controller's getInfo cache. This includes static GETINFO
# options (unchangable, even with a SETCONF) and other useful stats
CACHE_ARGS = ("version", "config-file", "exit-policy/default", "fingerprint",
              "config/names", "info/names", "features/names", "events/names",
              "nsEntry", "descEntry", "bwRate", "bwBurst", "bwObserved",
              "bwMeasured", "flags", "pid")

TOR_CTL_CLOSE_MSG = "Tor closed control connection. Exiting event thread."
UNKNOWN = "UNKNOWN" # value used by cached information if undefined
CONFIG = {"features.pathPrefix": "",
          "log.torCtlPortClosed": log.NOTICE,
          "log.torGetInfo": log.DEBUG,
          "log.torGetConf": log.DEBUG,
          "log.torPrefixPathInvalid": log.NOTICE}

# events used for controller functionality:
# NOTICE - used to detect when tor is shut down
# NEWDESC, NS, and NEWCONSENSUS - used for cache invalidation
REQ_EVENTS = {"NOTICE": "this will be unable to detect when tor is shut down",
              "NEWDESC": "information related to descriptors will grow stale",
              "NS": "information related to the consensus will grow stale",
              "NEWCONSENSUS": "information related to the consensus will grow stale"}

# provides int -> str mappings for torctl event runlevels
TORCTL_RUNLEVELS = dict([(val, key) for (key, val) in TorUtil.loglevels.items()])

def loadConfig(config):
  config.update(CONFIG)
  
  # make sure the path prefix is valid and exists (providing a notice if not)
  prefixPath = CONFIG["features.pathPrefix"].strip()
  
  if prefixPath:
    if prefixPath.endswith("/"): prefixPath = prefixPath[:-1]
    
    if prefixPath and not os.path.exists(prefixPath):
      msg = "The prefix path set in your config (%s) doesn't exist." % prefixPath
      log.log(CONFIG["log.torPrefixPathInvalid"], msg)
      prefixPath = ""
  
  CONFIG["features.pathPrefix"] = prefixPath

def getPathPrefix():
  """
  Provides the path prefix that should be used for fetching tor resources.
  """
  
  return CONFIG["features.pathPrefix"]

def getPid(controlPort=9051, pidFilePath=None):
  """
  Attempts to determine the process id for a running tor process, using the
  following:
  1. GETCONF PidFile
  2. "pidof tor"
  3. "netstat -npl | grep 127.0.0.1:%s" % <tor control port>
  4. "ps -o pid -C tor"
  
  If pidof or ps provide multiple tor instances then their results are
  discarded (since only netstat can differentiate using the control port). This
  provides None if either no running process exists or it can't be determined.
  
  Arguments:
    controlPort - control port of the tor process if multiple exist
    pidFilePath - path to the pid file generated by tor
  """
  
  # attempts to fetch via the PidFile, failing if:
  # - the option is unset
  # - unable to read the file (such as insufficient permissions)
  
  if pidFilePath:
    try:
      pidFile = open(pidFilePath, "r")
      pidEntry = pidFile.readline().strip()
      pidFile.close()
      
      if pidEntry.isdigit(): return pidEntry
    except: pass
  
  # attempts to resolve using pidof, failing if:
  # - tor's running under a different name
  # - there's multiple instances of tor
  try:
    results = sysTools.call("pidof tor")
    if len(results) == 1 and len(results[0].split()) == 1:
      pid = results[0].strip()
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using netstat, failing if:
  # - tor's being run as a different user due to permissions
  try:
    results = sysTools.call("netstat -npl | grep 127.0.0.1:%i" % controlPort)
    
    if len(results) == 1:
      results = results[0].split()[6] # process field (ex. "7184/tor")
      pid = results[:results.find("/")]
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using ps, failing if:
  # - tor's running under a different name
  # - there's multiple instances of tor
  try:
    results = sysTools.call("ps -o pid -C tor")
    if len(results) == 2:
      pid = results[1].strip()
      if pid.isdigit(): return pid
  except IOError: pass
  
  return None

def getConn():
  """
  Singleton constructor for a Controller. Be aware that this start
  uninitialized, needing a TorCtl instance before it's fully functional.
  """
  
  global CONTROLLER
  if CONTROLLER == None: CONTROLLER = Controller()
  return CONTROLLER

class Controller(TorCtl.PostEventListener):
  """
  TorCtl wrapper providing convenience functions, listener functionality for
  tor's state, and the capability for controller connections to be restarted
  if closed.
  """
  
  def __init__(self):
    TorCtl.PostEventListener.__init__(self)
    self.conn = None                    # None if uninitialized or controller's been closed
    self.connLock = threading.RLock()
    self.eventListeners = []            # instances listening for tor controller events
    self.torctlListeners = []           # callback functions for TorCtl events
    self.statusListeners = []           # callback functions for tor's state changes
    self.controllerEvents = []          # list of successfully set controller events
    self._isReset = False               # internal flag for tracking resets
    self._status = TOR_CLOSED           # current status of the attached control port
    self._statusTime = 0                # unix time-stamp for the duration of the status
    self.lastHeartbeat = 0              # time of the last tor event
    
    # cached getInfo parameters (None if unset or possibly changed)
    self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
    
    # directs TorCtl to notify us of events
    TorUtil.logger = self
    TorUtil.loglevel = "DEBUG"
  
  def init(self, conn=None):
    """
    Uses the given TorCtl instance for future operations, notifying listeners
    about the change.
    
    Arguments:
      conn - TorCtl instance to be used, if None then a new instance is fetched
             via the connect function
    """
    
    if conn == None:
      conn = TorCtl.connect()
      
      if conn == None: raise ValueError("Unable to initialize TorCtl instance.")
    
    if conn.is_live() and conn != self.conn:
      self.connLock.acquire()
      
      if self.conn: self.close() # shut down current connection
      self.conn = conn
      self.conn.add_event_listener(self)
      for listener in self.eventListeners: self.conn.add_event_listener(listener)
      
      # sets the events listened for by the new controller (incompatible events
      # are dropped with a logged warning)
      self.setControllerEvents(self.controllerEvents)
      
      self.connLock.release()
      
      self._status = TOR_INIT
      self._statusTime = time.time()
      
      # notifies listeners that a new controller is available
      thread.start_new_thread(self._notifyStatusListeners, (TOR_INIT,))
  
  def close(self):
    """
    Closes the current TorCtl instance and notifies listeners.
    """
    
    self.connLock.acquire()
    if self.conn:
      self.conn.close()
      self.conn = None
      self.connLock.release()
      
      self._status = TOR_CLOSED
      self._statusTime = time.time()
      
      # notifies listeners that the controller's been shut down
      thread.start_new_thread(self._notifyStatusListeners, (TOR_CLOSED,))
    else: self.connLock.release()
  
  def isAlive(self):
    """
    Returns True if this has been initialized with a working TorCtl instance,
    False otherwise.
    """
    
    self.connLock.acquire()
    
    result = False
    if self.conn:
      if self.conn.is_live(): result = True
      else: self.close()
    
    self.connLock.release()
    return result
  
  def getHeartbeat(self):
    """
    Provides the time of the last registered tor event (if listening for BW
    events then this should occure every second if relay's still responsive).
    This returns zero if this has never received an event.
    """
    
    return self.lastHeartbeat
  
  def getTorCtl(self):
    """
    Provides the current TorCtl connection. If unset or closed then this
    returns None.
    """
    
    self.connLock.acquire()
    result = None
    if self.isAlive(): result = self.conn
    self.connLock.release()
    
    return result
  
  def getInfo(self, param, default = None, suppressExc = True):
    """
    Queries the control port for the given GETINFO option, providing the
    default if the response is undefined or fails for any reason (error
    response, control port closed, initiated, etc).
    
    Arguments:
      param       - GETINFO option to be queried
      default     - result if the query fails and exception's suppressed
      suppressExc - suppresses lookup errors (returning the default) if true,
                    otherwise this raises the original exception
    """
    
    self.connLock.acquire()
    
    startTime = time.time()
    result, raisedExc, isFromCache = default, None, False
    if self.isAlive():
      if param in CACHE_ARGS and self._cachedParam[param]:
        result = self._cachedParam[param]
        isFromCache = True
      else:
        try:
          getInfoVal = self.conn.get_info(param)[param]
          if getInfoVal != None: result = getInfoVal
        except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed), exc:
          if type(exc) == TorCtl.TorCtlClosed: self.close()
          raisedExc = exc
    
    if not isFromCache and result and param in CACHE_ARGS:
      self._cachedParam[param] = result
    
    runtimeLabel = "cache fetch" if isFromCache else "runtime: %0.4f" % (time.time() - startTime)
    msg = "GETINFO %s (%s)" % (param, runtimeLabel)
    log.log(CONFIG["log.torGetInfo"], msg)
    
    self.connLock.release()
    
    if not suppressExc and raisedExc: raise raisedExc
    else: return result
  
  # TODO: This could have client side caching if there were events to indicate
  # SETCONF events. See:
  # https://trac.torproject.org/projects/tor/ticket/1692
  def getOption(self, param, default = None, multiple = False, suppressExc = True):
    """
    Queries the control port for the given configuration option, providing the
    default if the response is undefined or fails for any reason. If multiple
    values exist then this arbitrarily returns the first unless the multiple
    flag is set.
    
    Arguments:
      param       - configuration option to be queried
      default     - result if the query fails and exception's suppressed
      multiple    - provides a list of results if true, otherwise this just
                    returns the first value
      suppressExc - suppresses lookup errors (returning the default) if true,
                    otherwise this raises the original exception
    """
    
    self.connLock.acquire()
    
    startTime = time.time()
    result, raisedExc = [], None
    if self.isAlive():
      try:
        if multiple:
          for key, value in self.conn.get_option(param):
            if value != None: result.append(value)
        else:
          getConfVal = self.conn.get_option(param)[0][1]
          if getConfVal != None: result = getConfVal
      except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed), exc:
        if type(exc) == TorCtl.TorCtlClosed: self.close()
        result, raisedExc = default, exc
    
    msg = "GETCONF %s (runtime: %0.4f)" % (param, time.time() - startTime)
    log.log(CONFIG["log.torGetConf"], msg)
    
    self.connLock.release()
    
    if not suppressExc and raisedExc: raise raisedExc
    elif result == []: return default
    else: return result
  
  def getMyNetworkStatus(self, default = None):
    """
    Provides the network status entry for this relay if available. This is
    occasionally expanded so results may vary depending on tor's version. For
    0.2.2.13 they contained entries like the following:
    
    r caerSidi p1aag7VwarGxqctS7/fS0y5FU+s 9On1TRGCEpljszPpJR1hKqlzaY8 2010-05-26 09:26:06 76.104.132.98 9001 0
    s Fast HSDir Named Running Stable Valid
    w Bandwidth=25300
    p reject 1-65535
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("nsEntry", default)
  
  def getMyDescriptor(self, default = None):
    """
    Provides the descriptor entry for this relay if available.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("descEntry", default)
  
  def getMyBandwidthRate(self, default = None):
    """
    Provides the effective relaying bandwidth rate of this relay. Currently
    this doesn't account for SETCONF events.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwRate", default)
  
  def getMyBandwidthBurst(self, default = None):
    """
    Provides the effective bandwidth burst rate of this relay. Currently this
    doesn't account for SETCONF events.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwBurst", default)
  
  def getMyBandwidthObserved(self, default = None):
    """
    Provides the relay's current observed bandwidth (the throughput determined
    from historical measurements on the client side). This is used in the
    heuristic used for path selection if the measured bandwidth is undefined.
    This is fetched from the descriptors and hence will get stale if
    descriptors aren't periodically updated.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwObserved", default)
  
  def getMyBandwidthMeasured(self, default = None):
    """
    Provides the relay's current measured bandwidth (the throughput as noted by
    the directory authorities and used by clients for relay selection). This is
    undefined if not in the consensus or with older versions of Tor. Depending
    on the circumstances this can be from a variety of things (observed,
    measured, weighted measured, etc) as described by:
    https://trac.torproject.org/projects/tor/ticket/1566
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwMeasured", default)
  
  def getMyFlags(self, default = None):
    """
    Provides the flags held by this relay.
    
    Arguments:
      default - result if the query fails or this relay isn't a part of the consensus yet
    """
    
    return self._getRelayAttr("flags", default)
  
  def getMyPid(self):
    """
    Provides the pid of the attached tor process (None if no controller exists
    or this can't be determined).
    """
    
    return self._getRelayAttr("pid", None)
  
  def getStatus(self):
    """
    Provides a tuple consisting of the control port's current status and unix
    time-stamp for when it became this way (zero if no status has yet to be
    set).
    """
    
    return (self._status, self._statusTime)
  
  def addEventListener(self, listener):
    """
    Directs further tor controller events to callback functions of the
    listener. If a new control connection is initialized then this listener is
    reattached.
    
    Arguments:
      listener - TorCtl.PostEventListener instance listening for events
    """
    
    self.connLock.acquire()
    self.eventListeners.append(listener)
    if self.isAlive(): self.conn.add_event_listener(listener)
    self.connLock.release()
  
  def addTorCtlListener(self, callback):
    """
    Directs further TorCtl events to the callback function. Events are composed
    of a runlevel and message tuple.
    
    Arguments:
      callback - functor that'll accept the events, expected to be of the form:
                 myFunction(runlevel, msg)
    """
    
    self.torctlListeners.append(callback)
  
  def addStatusListener(self, callback):
    """
    Directs further events related to tor's controller status to the callback
    function.
    
    Arguments:
      callback - functor that'll accept the events, expected to be of the form:
                 myFunction(controller, eventType)
    """
    
    self.statusListeners.append(callback)
  
  def removeStatusListener(self, callback):
    """
    Stops listener from being notified of further events. This returns true if a
    listener's removed, false otherwise.
    
    Arguments:
      callback - functor to be removed
    """
    
    if callback in self.statusListeners:
      self.statusListeners.remove(callback)
      return True
    else: return False
  
  def getControllerEvents(self):
    """
    Provides the events the controller's currently configured to listen for.
    """
    
    return list(self.controllerEvents)
  
  def setControllerEvents(self, events):
    """
    Sets the events being requested from any attached tor instance, logging
    warnings for event types that aren't supported (possibly due to version
    issues). Events in REQ_EVENTS will also be included, logging at the error
    level with an additional description in case of failure.
    
    This remembers the successfully set events and tries to request them from
    any tor instance it attaches to in the future too (again logging and
    dropping unsuccessful event types).
    
    This returns the listing of event types that were successfully set. If not
    currently attached to a tor instance then all events are assumed to be ok,
    then attempted when next attached to a control port.
    
    Arguments:
      events - listing of events to be set
    """
    
    self.connLock.acquire()
    
    returnVal = []
    if self.isAlive():
      events = set(events)
      events = events.union(set(REQ_EVENTS.keys()))
      unavailableEvents = set()
      
      # removes anything we've already failed to set
      if DROP_FAILED_EVENTS:
        unavailableEvents.update(events.intersection(FAILED_EVENTS))
        events.difference_update(FAILED_EVENTS)
      
      # initial check for event availability, using the 'events/names' GETINFO
      # option to detect invalid events
      validEvents = self.getInfo("events/names")
      
      if validEvents:
        validEvents = set(validEvents.split())
        unavailableEvents.update(events.difference(validEvents))
        events.intersection_update(validEvents)
      
      # attempt to set events via trial and error
      isEventsSet, isAbandoned = False, False
      
      while not isEventsSet and not isAbandoned:
        try:
          self.conn.set_events(list(events))
          isEventsSet = True
        except TorCtl.ErrorReply, exc:
          msg = str(exc)
          
          if "Unrecognized event" in msg:
            # figure out type of event we failed to listen for
            start = msg.find("event \"") + 7
            end = msg.rfind("\"")
            failedType = msg[start:end]
            
            unavailableEvents.add(failedType)
            events.discard(failedType)
          else:
            # unexpected error, abandon attempt
            isAbandoned = True
        except TorCtl.TorCtlClosed:
          self.close()
          isAbandoned = True
      
      FAILED_EVENTS.update(unavailableEvents)
      if not isAbandoned:
        # logs warnings or errors for failed events
        for eventType in unavailableEvents:
          defaultMsg = DEFAULT_FAILED_EVENT_MSG % eventType
          if eventType in REQ_EVENTS:
            log.log(log.ERR, defaultMsg + " (%s)" % REQ_EVENTS[eventType])
          else:
            log.log(log.WARN, defaultMsg)
        
        self.controllerEvents = list(events)
        returnVal = list(events)
    else:
      # attempts to set the events when next attached to a control port
      self.controllerEvents = list(events)
      returnVal = list(events)
    
    self.connLock.release()
    return returnVal
  
  def reload(self, issueSighup = False):
    """
    This resets tor (sending a RELOAD signal to the control port) causing tor's
    internal state to be reset and the torrc reloaded. This can either be done
    by...
      - the controller via a RELOAD signal (default and suggested)
          conn.send_signal("RELOAD")
      - system reload signal (hup)
          pkill -sighup tor
    
    The later isn't really useful unless there's some reason the RELOAD signal
    won't do the trick. Both methods raise an IOError in case of failure.
    
    Arguments:
      issueSighup - issues a sighup rather than a controller RELOAD signal
    """
    
    self.connLock.acquire()
    
    raisedException = None
    if self.isAlive():
      if not issueSighup:
        try:
          self.conn.send_signal("RELOAD")
          self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
        except Exception, exc:
          # new torrc parameters caused an error (tor's likely shut down)
          # BUG: this doesn't work - torrc errors still cause TorCtl to crash... :(
          # http://bugs.noreply.org/flyspray/index.php?do=details&id=1329
          raisedException = IOError(str(exc))
      else:
        try:
          # Redirects stderr to stdout so we can check error status (output
          # should be empty if successful). Example error:
          # pkill: 5592 - Operation not permitted
          #
          # note that this may provide multiple errors, even if successful,
          # hence this:
          #   - only provide an error if Tor fails to log a sighup
          #   - provide the error message associated with the tor pid (others
          #     would be a red herring)
          if not sysTools.isAvailable("pkill"):
            raise IOError("pkill command is unavailable")
          
          self._isReset = False
          pkillCall = os.popen("pkill -sighup ^tor$ 2> /dev/stdout")
          pkillOutput = pkillCall.readlines()
          pkillCall.close()
          
          # Give the sighupTracker a moment to detect the sighup signal. This
          # is, of course, a possible concurrency bug. However I'm not sure
          # of a better method for blocking on this...
          waitStart = time.time()
          while time.time() - waitStart < 1:
            time.sleep(0.1)
            if self._isReset: break
          
          if not self._isReset:
            errorLine, torPid = "", self.getMyPid()
            if torPid:
              for line in pkillOutput:
                if line.startswith("pkill: %s - " % torPid):
                  errorLine = line
                  break
            
            if errorLine: raise IOError(" ".join(errorLine.split()[3:]))
            else: raise IOError("failed silently")
          
          self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
        except IOError, exc:
          raisedException = exc
    
    self.connLock.release()
    
    if raisedException: raise raisedException
  
  def msg_event(self, event):
    """
    Listens for reload signal (hup), which is either produced by:
    causing the torrc and internal state to be reset.
    """
    
    if event.level == "NOTICE" and event.msg.startswith("Received reload signal (hup)"):
      self._isReset = True
      
      self._status = TOR_INIT
      self._statusTime = time.time()
      
      thread.start_new_thread(self._notifyStatusListeners, (TOR_INIT,))
  
  def ns_event(self, event):
    self._updateHeartbeat()
    
    myFingerprint = self.getInfo("fingerprint")
    if myFingerprint:
      for ns in event.nslist:
        if ns.idhex == myFingerprint:
          self._cachedParam["nsEntry"] = None
          self._cachedParam["flags"] = None
          self._cachedParam["bwMeasured"] = None
          return
    else:
      self._cachedParam["nsEntry"] = None
      self._cachedParam["flags"] = None
      self._cachedParam["bwMeasured"] = None
  
  def new_consensus_event(self, event):
    self._updateHeartbeat()
    
    self._cachedParam["nsEntry"] = None
    self._cachedParam["flags"] = None
    self._cachedParam["bwMeasured"] = None
  
  def new_desc_event(self, event):
    self._updateHeartbeat()
    
    myFingerprint = self.getInfo("fingerprint")
    if not myFingerprint or myFingerprint in event.idlist:
      self._cachedParam["descEntry"] = None
      self._cachedParam["bwObserved"] = None
  
  def circ_status_event(self, event):
    self._updateHeartbeat()
  
  def buildtimeout_set_event(self, event):
    self._updateHeartbeat()
  
  def stream_status_event(self, event):
    self._updateHeartbeat()
  
  def or_conn_status_event(self, event):
    self._updateHeartbeat()
  
  def stream_bw_event(self, event):
    self._updateHeartbeat()
  
  def bandwidth_event(self, event):
    self._updateHeartbeat()
  
  def address_mapped_event(self, event):
    self._updateHeartbeat()
  
  def unknown_event(self, event):
    self._updateHeartbeat()
  
  def log(self, level, msg, *args):
    """
    Tracks TorCtl events. Ugly hack since TorCtl/TorUtil.py expects a
    logging.Logger instance.
    """
    
    # notifies listeners of TorCtl events
    for callback in self.torctlListeners: callback(TORCTL_RUNLEVELS[level], msg)
    
    # checks if TorCtl is providing a notice that control port is closed
    if TOR_CTL_CLOSE_MSG in msg: self.close()
  
  def _updateHeartbeat(self):
    """
    Called on any event occurance to note the time it occured.
    """
    
    # alternative is to use the event's timestamp (via event.arrived_at)
    self.lastHeartbeat = time.time()
  
  def _getRelayAttr(self, key, default, cacheUndefined = True):
    """
    Provides information associated with this relay, using the cached value if
    available and otherwise looking it up.
    
    Arguments:
      key            - parameter being queried (from CACHE_ARGS)
      default        - value to be returned if undefined
      cacheUndefined - caches when values are undefined, avoiding further
                       lookups if true
    """
    
    currentVal = self._cachedParam[key]
    if currentVal:
      if currentVal == UNKNOWN: return default
      else: return currentVal
    
    self.connLock.acquire()
    
    currentVal, result = self._cachedParam[key], None
    if not currentVal and self.isAlive():
      # still unset - fetch value
      if key in ("nsEntry", "descEntry"):
        myFingerprint = self.getInfo("fingerprint")
        
        if myFingerprint:
          queryType = "ns" if key == "nsEntry" else "desc"
          queryResult = self.getInfo("%s/id/%s" % (queryType, myFingerprint))
          if queryResult: result = queryResult.split("\n")
      elif key == "bwRate":
        # effective relayed bandwidth is the minimum of BandwidthRate,
        # MaxAdvertisedBandwidth, and RelayBandwidthRate (if set)
        effectiveRate = int(self.getOption("BandwidthRate"))
        
        relayRate = self.getOption("RelayBandwidthRate")
        if relayRate and relayRate != "0":
          effectiveRate = min(effectiveRate, int(relayRate))
        
        maxAdvertised = self.getOption("MaxAdvertisedBandwidth")
        if maxAdvertised: effectiveRate = min(effectiveRate, int(maxAdvertised))
        
        result = effectiveRate
      elif key == "bwBurst":
        # effective burst (same for BandwidthBurst and RelayBandwidthBurst)
        effectiveBurst = int(self.getOption("BandwidthBurst"))
        
        relayBurst = self.getOption("RelayBandwidthBurst")
        if relayBurst and relayBurst != "0":
          effectiveBurst = min(effectiveBurst, int(relayBurst))
        
        result = effectiveBurst
      elif key == "bwObserved":
        for line in self.getMyDescriptor([]):
          if line.startswith("bandwidth"):
            # line should look something like:
            # bandwidth 40960 102400 47284
            comp = line.split()
            
            if len(comp) == 4 and comp[-1].isdigit():
              result = int(comp[-1])
              break
      elif key == "bwMeasured":
        # TODO: Currently there's no client side indication of what type of
        # measurement was used. Include this in results if it's ever available.
        
        for line in self.getMyNetworkStatus([]):
          if line.startswith("w Bandwidth="):
            bwValue = line[12:]
            if bwValue.isdigit(): result = int(bwValue)
            break
      elif key == "flags":
        for line in self.getMyNetworkStatus([]):
          if line.startswith("s "):
            result = line[2:].split()
            break
      elif key == "pid":
        result = getPid(int(self.getOption("ControlPort", 9051)), self.getOption("PidFile"))
      
      # cache value
      if result: self._cachedParam[key] = result
      elif cacheUndefined: self._cachedParam[key] = UNKNOWN
    elif currentVal == UNKNOWN: result = currentVal
    
    self.connLock.release()
    
    if result: return result
    else: return default
  
  def _notifyStatusListeners(self, eventType):
    """
    Sends a notice to all current listeners that a given change in tor's
    controller status has occurred.
    
    Arguments:
      eventType - enum representing tor's new status
    """
    
    # resets cached getInfo parameters
    self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
    
    # gives a notice that the control port has closed
    if eventType == TOR_CLOSED:
      log.log(CONFIG["log.torCtlPortClosed"], "Tor control port closed")
    
    for callback in self.statusListeners:
      callback(self, eventType)

