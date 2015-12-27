import os
import sys
import sqlite3
#import MySQLdb
import json
import logging
import datetime
import time
import bisect
import random
import importlib
import threading
from itertools import ifilter, chain
from wrapt.decorators import synchronized
from extensionmatchconstants import *
from tournaments import Tournaments
from kingofthehill import KingOfTheHill
from quickmatch import QuickMatch



# Dict of extensions with unique identifiers.
#       Individual extensions may be disabled universally by removing them from this list and restarting the server.  When this
#       is done none of the extension's server or client code will run and their UI will not display in any client.
_ExtensionDict = {
                          # ID 0 is reserved
                          1: Tournaments,
                          2: KingOfTheHill,
                          3: QuickMatch,
                       }

_Admins = ["pof", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v",
                "w", "x", "y", "z"]

# _dbfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])), 'db', 'extensionuserdata.sqlite3')

GameVariableData = dict()
"""
GameVariableData is only used by the client, but is stored here and sent to the client during login so that adding a new
game doesn't require a client update.

This dict maps a channel name to a 2-tuple.  The first element is a list of bytes to search for within the emulator's memory
that will identify the location of the game's memory.  The second element is a list of 2-tuples containing the offset of
game data fields from the bytes in the first element, and the length of the field in bytes.  Each variable is assumed to be
a 1, 2, or 4 byte integer stored in little-endian format.  In particular, the field size must be 1, 2, or 4.

The absolute location of the game data within the emulator's memory is not consistent, but always seems to be contiguous.
Therefore to locate it, we search for an identifying sequence of bytes within it and locate the variables we are interested
in by working from there.  The existence of an unchanging, distinctive sequence of bytes within the game data is true for
SF3:3S, but might not be true for other games.  A more robust solution would be to modify the emulator to insert some sort of
beacon at the start of the game data (possibly the quark ID?), that could be searched for instead.

To find and calculate the locations of game variables, I recommend Cheat Engine (cheatengine.org).

To add a new game, add an entry to GameVariableData with the relevant memory info, and create a _checkEvents_<channel name>
method in _GameSession to translate the memory info received from clients into events (see _GameSession for more details).

IMPORTANT: When adding games, the first data field listed must be the frame number.  The order/identity of the rest aren't
important, as long as the _GameSession._checkEvents_<channel name> has the info needed to generate events.
"""
GameVariableData['sfiii3n'] = (
                                [0x78, 0x00, 0x79, 0x00, 0x76, 0x00],
                                [
                                    (-0x60AA0,4), #frame number
                                    (-0x51CC6,1), #win streak player 1
                                    (-0x51CC8,1), #win streak player 2
                                    (-0x57620,1), #rounds won this game player 1
                                    (-0x5761A,1), #rounds won this game player 2
                                    (-0x5762C,1), #timer
                                    (0x368,1),    #health player 1 (out of 160)
                                    (0x800,1),    #health player 2 (out of 160)
                                ]
                             )

# Fields are assumed to be integers stored little-endian and 1,2, or 4 bytes in length.  If other cases are needed later, the
# client will need to be updated.
assert set([x[1] for c in GameVariableData.values() for x in c[1]]) <= {1,2,4}

class Extension(object):
    """ Initializes extensions and handles communication between client extensions and server extensions"""

    ggposerver = None
    ChannelJoin=ChannelLeaving=EmulatorClosed=MatchStarting=StatusChanged=None

    _dbconnect = _dbBinary = None
    _writeUserDataSql = ""
    _readUserDataSql = ""
    _instanceDict = {}  # maps extension ID to a dictionary of extension instances keyed by channel name
    _codeToString = {}

    # Data blob with initialization information to send to clients on startup.
    _initData = [
                    _ExtensionDict.keys(),
                    GameVariableData,
                    [(ext.chatCommand, extID) for extID, ext in _ExtensionDict.iteritems() if ext.chatCommand != None],
                ]

    _playerColor = {}  # maps (channel, username) to the color that player should appear as in player lists

    @classmethod
    def Initialize(cls, ggposerver, dbconnect, DB_ENGINE, PARAM):
        """ Initialize some things and instantiate all our extension instances"""

        cls.ggposerver = ggposerver

        cls._dbconnect = staticmethod(dbconnect)
        cls._dbBinary = staticmethod(sqlite3.Binary if DB_ENGINE == "sqlite3" else (lambda x: x))

        cls._writeUserDataSql = "REPLACE INTO extensionuserdata VALUES ({0}, {0}, {0}, {0})".format(PARAM)
        cls._readUserDataSql = "SELECT data FROM extensionuserdata WHERE extid={0} AND name={0} AND channel={0}".format(PARAM)

        cls.ChannelJoin = _DerpEvent()
        cls.StatusChanged = _DerpEvent()
        cls.ChannelLeaving = _DerpEvent()
        cls.EmulatorClosed = _DerpEvent()
        cls.MatchStarting = _DerpEvent()

        # make a dictionary translating message codes to message names just to make the logs easier to follow
        cls._codeToString = {(0, val):nm for nm, val in _Message.__dict__.iteritems() if nm[0] != "_"}

        for extID, ext in _ExtensionDict.iteritems():
            try:
                messageEnum = importlib.import_module(ext.__module__).Message
                cls._codeToString.update({(extID, val):nm for nm, val in messageEnum.__dict__.iteritems() if nm[0] != "_"})
            except:
                pass

            # Fill in some blanks within ExtensionBase before initialization
            ext.Initialize(extID=extID, admins=_Admins, extensionIO=cls)

            # One instance for each channel
            cls._instanceDict[extID] = {ggpochannel.name: ext(ggpochannel) for ggpochannel in cls.ggposerver.channels.values()}

            # Give the extension handles to all its instances
            ext.setInstances(cls._instanceDict[extID])

            # In case extension needs to do additional initialization after all instances have been created
            ext.onInstantiationEnded()

    @classmethod
    def parseMessage(cls, client, cmd):
        """ Decode and route a message received from a client

        Message is encoded as follows:
            byte 0-3 - positive integer, 0 for internal messages, 1 for referee messages, otherwise the extension ID of
                       the extension to receive the message.
            byte 4-7 - extension-defined positive integer
            byte 8+ - json-packed python object parameter

        Args:
            client: Sending client
            cmd: Encoded message data
        """
        extID = int(cmd[0:4].encode('hex'), 16)
        prefix = int(cmd[4:8].encode('hex'), 16)
        params = None if cmd[8:] == '' else json.loads(cmd[8:])

        dispext = "Extension" if extID == 0 else _ExtensionDict[extID].__name__
        dispprefix = cls._codeToString.get((extID, prefix), prefix)

        # DEBUG
        print "Extension Message Received: From %s to %s: prefix=%s, params=%s" % (client.nick, dispext, dispprefix, params)
        logging.info("Extension Message Received: From %s to %s: prefix=%s, params=%s" % (client.nick, dispext, dispprefix, params))

        if extID == 0:
            # Internal messages
            if prefix == _Message.InitializationDataRequest:
                # A client has logged on and is requesting initialization info
                cls.sendMessage(0, client, _Message.InitializationDataResponse, cls._initData)
#                cls.Logon.emit(client.channel.name, client)

            elif prefix == _Message.AnchorClicked:
                # User clicked a text anchor created by us or by an extension
                try:
                    if params[0] == 0:
                        cls._receiveAnchorClick(client, params[1])

                    elif params[0] in cls._instanceDict and client.channel.name in cls._instanceDict[params[0]]:
                        cls._instanceDict[params[0]][client.channel.name].receiveAnchorClick(client, params[1])

                except:
                    pass

            elif prefix == _Message.ChatCommand:
                # user entered a "/foo bar" type chat command
                if params[0] in cls._instanceDict and client.channel.name in cls._instanceDict[params[0]]:
                    cls._instanceDict[params[0]][client.channel.name].receiveChatCommand(client, params[1])

            elif prefix == _Message.MatchEvent:
                match = ExtensionMatch.matchFromClient(client)
                if match != None:
                    match.update(client, evt=params[0], evtparams=tuple(params[1:]))

            elif prefix == _Message.MatchData:
                match = ExtensionMatch.matchFromClient(client)
                if match != None:
                    match.update(client, values=params)

            else:
                pass

        elif extID in cls._instanceDict and client.channel.name in cls._instanceDict[extID]:
            # Extension messages
            cls._instanceDict[extID][client.channel.name].receiveMessage(client, prefix, params)

        else:
            pass

    @classmethod
    def _receiveAnchorClick(cls, client, cmd):
        arr = cmd.split(",")
        if arr[0] == "as":
            # add user to auto-spectate pool
            match = ExtensionMatch.matchFromClient(cls.ggposerver.clients.get(arr[1]))
            if match != None:
                match.spectate([client])
                cls.chatMessage(client, _SpecStrings.autospectateOK % cls.buildAnchorString("rs,%s,%s,%s" % (arr[1], arr[2], arr[3]), "here"))
            else:
                cls.chatMessage(client, _SpecStrings.autospectateError)

        elif arr[0] == "rs":
            # remove from autospectate pool
            match = ExtensionMatch.matchFromClient(cls.ggposerver.clients.get(arr[1]))
            if match != None:
                match.unspectate([client])
            cls.chatMessage(client, _SpecStrings.autospectateRemoved)

    @classmethod
    def sendMessage(cls, extID, client, prefix, params=None):
        """ Encode message to transmit to client """
        if client == None:
            return

        data = client.pad2hex(extID) + client.pad2hex(prefix) + json.dumps(params)
        negseq = 4294967277  # '\xff\xff\xff\xed'
        pdu = client.sizepad(data)
        response = client.reply(negseq, pdu)
        logging.debug('to %s: %r' % (client.client_ident(), response))
        client.send_queue.append(response)

        dispext = "Extension" if extID == 0 else _ExtensionDict[extID].__name__
        dispprefix = cls._codeToString.get((extID, prefix), prefix)

        # DEBUG
        print "Extension Message Sent: To %s from %s: prefix=%s, params=%s" % (client.nick, dispext, dispprefix, params)
        logging.info("Extension Message Sent: To %s from %s: prefix=%s, params=%s" % (client.nick, dispext, dispprefix, params))

    @classmethod
    def chatMessage(cls, client, msg):
        """ Tell client to display string in chat """
        cls.sendMessage(0, client, _Message.ChatMessage, msg)

    @classmethod
    def writeUserData(cls, extID, name, data, channel):
        """ Store object in data table for later retrieval """
        try:
            pdata = json.dumps(data)
        except:
            try:
                logging.error("Serialization error in writeUserData. extID=%s, name=%s, channel=%s, data=%s" % (extID, name, channel, data))
            except:
                logging.error("Serialization error in writeUserData. extID=%s, name=%s, channel=%s" % (extID, name, channel))
            return

        conn = cls._dbconnect()
        cursor = conn.cursor()
        cursor.execute(cls._writeUserDataSql, [extID, name, channel, cls._dbBinary(pdata)])
        conn.commit()
        conn.close()

    @classmethod
    def readUserData(cls, extID, name, channel):
        """ Retrieve object stored with writeUserData """
        conn = cls._dbconnect()
        cursor = conn.cursor()
        cursor.execute(cls._readUserDataSql, [extID, name, channel])
        data = cursor.fetchone()
        conn.close()

        try:
            return None if data == None else json.loads(str(data[0]))
        except:
            return None

    @classmethod
    def canSpectate(cls, client, name1, name2):
        """ client is trying to spectate a match between name1 and name2 (i.e. double clicked on it on the match list).  Tell
            the server whether we're OK with that.
        """

        match = ExtensionMatch.matchFromClient(cls.ggposerver.clients.get(name1))
        if match == None:
            return True
        elif match.canSpectateNow():
            return True
        elif not match.canSpectateEver():
            cls.chatMessage(client, _SpecStrings.spectatingForbidden)
            return False
        else:
            cls.chatMessage(client, _SpecStrings.spectatingRestricted % cls.buildAnchorString("as,%s,%s,%s" % (name1, name2, client.channel.name), "here"))
            return False

    @classmethod
    def setPlayerColor(cls, channel, username, color):
        if color == None or color > 0xFFFFFF or color < 0:
            cls._playerColor.pop((channel, username), None)
        else:
            cls._playerColor[(channel, username)] = color
            client = cls.ggposerver.clients.get(username)
            if client != None and client.channel.name == channel:
                client.handle_status((client.status, 0))  # refresh color on players' screens

    @classmethod
    def getPlayerColor(cls, client):
        """ If a player/match should be displayed in a custom color, find it and return it """

        match = ExtensionMatch.matchFromClient(client)
        if match != None and client.opponent != None:
            return match.color
        else:
            return cls._playerColor.get((client.channel.name, client.nick), 0x1000000)

    @classmethod
    def buildAnchorString(cls, key, linktext):
        """Format a clickable link to display in chat.

        Args:
            key: a string
            linktext: the displayed text to be hotlinked

        Returns: A string of the form "<a href=...>linkText</a>" which can be inserted into a chat message.  When clicked,
                    the value of key will be passed to receiveChatCommand() on the server.

        """
        return "<a href=extension:%s,%s>%s</a>" % (cls.extID, key, linktext)

    @classmethod
    def killEmulator(cls, client):
        """ Tell client to close emulator.

        Args:
            client: GGPOClient instance
        """
        cls.sendMessage(0, client, _Message.KillEmulator)

    @classmethod
    def createMatch(cls, *args, **kwargs):
        return ExtensionMatch(*args, **kwargs)

    @classmethod
    def Timer(cls, delay, target, args, kwargs):
        return _DerpTimer(delay, target, args, kwargs)

    @classmethod
    def monitoringAvailable(cls, channel):
        """Returns True or False according to whether event monitoring is available for the channel

        Does not gurantee that monitoring will be available for every match in this channel.

        Args:
            channel: channel name

        """
        return channel in GameVariableData

    @classmethod
    def scheduledTournaments(cls, channel, client):
        """ Return a list of scheduled tournaments in the indicated channel.

            Args:
                channel: channel name
                client: GGPOclient instance

            Returns:
                A possibly empty list of tuples (name, startTime) giving the tournament name and its start time
                in the client's local time zone.

                A currently running tournament is included only if registration is still in progress
        """
        if 'Tournaments' not in globals() or Tournaments not in _ExtensionDict.values():
            return []

        return Tournaments.scheduledTournaments(channel, client)

class ExtensionMatch(object):
    """ An object that represents a match between two clients.

        After creating and starting an ExtensionMatch object, a stream of events will be delivered one by one to the
        callback passed to the initializer.  These codes keep the extension updated on the success or failure of game startup,
        and - where possible - on actual in-game events such as game victories.  See the MatchEvent class for full details
        of the events that will be passed.

        This class will handles spectate requests from users according to the spectator policy set in the extension class (see
        spectatorPolicy member in ExtensionBase).  Extensions may add spectators by passing the spectator's client instance
        to the spectate() method, which will be added either immediately or during the next between-game break, depending on
        the spectator policy and whether we are able to monitor in-game events.

        *ABOUT IN-GAME MONITORING*

        In-game events are monitored by reading directly from the emulator's process memory using memory offsets calculated
        beforehand (the GameVariableData structure defined below).  This must be done client-side, since games are not
        emulated on the server.

        These addresses must be calculated separately for each game, and, for now, reading the emulator memory is only
        possible in Windows.  If the match being played is not a configured game or if neither player is using Windows,
        in-game events will not be sent.  This is indicated to the extension through an event passed to the callback (see
        MatchEvent definition).

        See client version of extension.py for details about how memory reading is done.
    """
    _instances = {}
    _extIO = Extension

    # noinspection PyDefaultArgument
    def __init__(self, extID, channel, clients, callback, randomizeSides=False, startupTimeout=0, startupRetries=0, spectators=set()):
        """

        Args:
            extID: ID number of owning extension
            channel: channel name
            clients: client instances of players
            callback: callback function to pass events to.  The callback must accept 3 arguments, which will be
                                1: this ExtensionMatch instance
                                2: event code (MatchEvent member)
                                3: tuple containing additional information (described in MatchEvent definition)
            randomizeSides: By default, the first element of clients as passed will be player 1.  Set this to True to
                            assign random sides instead.
            startupTimeout: Seconds to wait for emulation to succesfully begin before aborting and possibly retrying (if
                            startupRetries > 0)
            startupRetries: Number of times to retry if startup fails.
            spectators: Iterable with clients to add as spectators on match startup.

        """
        self._extID = extID                  # extension ID of owning extension
        self.channel = channel              # channel name
        self._ggpochannel = self._extIO.ggposerver.channels[self.channel]
        self.clients = list(clients)
        if randomizeSides:
            random.shuffle(self.clients)
        self.quark = None
        self._callback = callback            # callback to send events to
        self._startupTimeout = startupTimeout  # if match fails to start after this long, abort
        self._startupRetries = startupRetries  # number of times to retry startup
        self._events = {1:[], 2:[]}         # Log of events received from each client.
        self._calibrated = {1:None, 2:None} # Flags for each side.  True/False = can/can't monitor, None = don't know yet
        self._oldValues = {1:None, 2:None}  # Previous memory scan for each side
        self._gameActive = None             # Are we in the middle of a game right now?  True/False/None = Yes/No/Don't know
        self._sideUsed = random.randint(1,2) # Chose which client's scan information to use ahead of time.  This side will
                                             # be changed later if the chosen side can't monitor.
        self._emulationStarted = False      # has the emulation started yet?
        self._waitingToRestart = False      # true if previous start attempt failed and we are waiting to restart
        self._closed = False                # once set, no further action will be taken on anything
        self._queuedSpectators = set(spectators)  # list of clients to add as spectators at next opportunity
        self._startTimeoutTimer = None        # timer to abort match startup if a timeout was specified
        self._retryTimeoutTimer = None        # timer to abort startup retry if players don't become available
        self._calibrateTimeoutTimer = None    # timer to verify clients have indicated whether they can monitor the active
                                              # match in a reasonable amount of time

        # properties determined by owning extension
        self._spectatorPolicy = _ExtensionDict[extID].spectatorPolicy
        self.color = _ExtensionDict[extID].playerColorRGB

        # fields containing info on game status
        self.calibrated = None              # True if game monitoring is possible, False if not, None if we don't know yet
        self.timer = 0                      # round timer
        self.health = {1:0, 2:0}            # player health (integer between 0-100)
        self.roundScore = {1:0, 2:0}        # round score for current game
        self.gameScore = {1:0, 2:0}         # number of games won by each player since emulator was opened.

        # handler to translate memory scans into events
        self._updatehandler = getattr(self, '_analyze_%s' % self.channel, None)

        self._extIO.EmulatorClosed.connect(self.channel, self._onEmulatorClosed)
        self._extIO.MatchStarting.connect(self.channel, self._onMatchStarting)
        self._extIO.StatusChanged.connect(self.channel, self._onStatusChanged)
        self._extIO.ChannelLeaving.connect(self.channel, self._onChannelLeaving)

        try:
            self._start()
        except MatchError as e:
            self._emitEvents([(MatchEvent.StartupFailedNotRetrying, (e.code,))])
            self.close(False)
            raise

        logging.info("ExtensionMatch created between %s and %s for extension %s" %
                        (self.clients[0].nick, self.clients[1].nick, _ExtensionDict[extID].__name__))

    @synchronized
    def _start(self):

        avail = self._availability()
        if not avail[0] or not avail[1]:
            raise Exception
        if not avail[0] and not avail[1]:
            raise MatchError(MatchErrorCode.BothPlayersUnavailable)
        elif not avail[0]:
            raise MatchError(MatchErrorCode.P1Unavailable)
        elif not avail[1]:
            raise MatchError(MatchErrorCode.P2Unavailable)

        # Should be able to start now.

        if self._retryTimeoutTimer != None:
            self._retryTimeoutTimer.cancel()
        if self._waitingToRestart:
            self._startupRetries -= 1
        self._waitingToRestart = False

        self.clients[0].challenging[self.clients[1].host] = self.clients[1]
        self.clients[1].handle_accept((self.clients[0].nick, 0, 0))

        if self._startupTimeout > 0:
            self._startTimeoutTimer = _DerpTimer(self._startupTimeout, self._onStartTimeout)
            self._startTimeoutTimer.daemon = True
            self._startTimeoutTimer.start()

        self.quark = self.clients[0].quark
        self._instances[self.quark] = self


    @synchronized
    def close(self, killEmulators=True, delay=0, _emulatorCloser=0):
        """ Ends the event stream or aborts startup, and closes the game windows on both clients.

            Only necessary if ending a match prematurely.  A match that signals EmulatorClosed or StartupFailedNotRetrying
            is already "closed".

        Args:
            killEmulators: Set to False to skip closing the game windows.
            delay: Seconds to wait before actually closing
            _emulatorCloser: Internal use.  Side of player that closed emulator
        """
        if self._closed:
            return

        if delay > 0:
            thd = _DerpTimer(delay, self.close, [killEmulators, 0, _emulatorCloser])
            thd.daemon = True
            thd.start()
            return

        if self._retryTimeoutTimer != None:
            self._retryTimeoutTimer.cancel()
        if self._startTimeoutTimer != None:
            self._startTimeoutTimer.cancel()
        if self._calibrateTimeoutTimer != None:
            self._calibrateTimeoutTimer.cancel()

        self._waitingToRestart = False
        self._closed = True

        self._extIO.EmulatorClosed.disconnect(self.channel, self._onEmulatorClosed)
        self._extIO.MatchStarting.disconnect(self.channel, self._onMatchStarting)
        self._extIO.StatusChanged.disconnect(self.channel, self._onStatusChanged)
        self._extIO.ChannelLeaving.disconnect(self.channel, self._onChannelLeaving)

        logging.info("ExtensionMatch between %s and %s closing." % (self.clients[0].nick, self.clients[1].nick))

        if killEmulators:
            self._extIO.killEmulator(self.clients[0])
            self._extIO.killEmulator(self.clients[1])

        if self.quark != None:
            self._emitEvents([(MatchEvent.EmulatorClosed, (_emulatorCloser,))])

        self._instances.pop(self.quark, None)
        self.quark = None

    @synchronized
    def _onEmulatorClosed(self, client):
        """ If the emulation has already started, shut down.  Otherwise abort startup attempt and possibly retry"""
        if client in self.clients and client.quark == self.quark and self.quark != None and not self._closed:
            side = self.clients.index(client) + 1

            if self._emulationStarted:
                self.close(False, _emulatorCloser=side)
            else:
                self._matchStartFail(MatchErrorCode.P1ClosedEmulator if side==1 else MatchErrorCode.P2ClosedEmulator)

    @synchronized
    def _onMatchStarting(self, quarkobj):
        """ The game emulation has begun and we can signal the clients to begin collecting and reporting data """

        if quarkobj.quark != self.quark or self.quark == None or self._closed or self._emulationStarted:
            return

        self._emulationStarted = True
        if self._startTimeoutTimer != None:
            self._startTimeoutTimer.cancel()

        self._emitEvents([(MatchEvent.EmulationStarted, ())])
        self._extIO.sendMessage(0, quarkobj.p1client, _Message.BeginMonitoring)
        self._extIO.sendMessage(0, quarkobj.p2client, _Message.BeginMonitoring)

        # clients should respond to BeginMonitoring immediately with CalibrationFail or CalibrationSuccess, but just in case they don't...
        self._calibrateTimeoutTimer = _DerpTimer(3, self._onCalibrateTimeout)
        self._calibrateTimeoutTimer.daemon = True
        self._calibrateTimeoutTimer.start()

    @synchronized
    def _onStatusChanged(self, client):
        """ If we're in the middle of trying to restart, watch for signals from both clients that they're available again """

        if client in self.clients and self._waitingToRestart and False not in self._availability() and not self._closed:
            try:
                self._start()
            except MatchError:  # someone was available 3 lines ago but not anymore >:/
                return

    def _onChannelLeaving(self, client):
        if client in self.clients and not self._closed:
            self.close(_emulatorCloser=self.clients.index(client)+1)

    @synchronized
    def _onStartTimeout(self):
        """ Game hasn't started in the indicated time limit. """
        if self._closed or self._emulationStarted:
            return

        self._matchStartFail(MatchErrorCode.Timeout)

    @synchronized
    def _onRetryTimeout(self):
        """ We have waited 5 seconds since killing the emulators but players haven't become available.  Signal error and quit."""

        if self._closed or not self._waitingToRestart:
            return

        try:
            self._start()  # almost certainly will fail if we made it here, but simplest way to get the error code
        except MatchError as e:
            self._emitEvents([(MatchEvent.StartupFailedNotRetrying, (e.code,))])
            self.close(False)

    @synchronized
    def _onCalibrateTimeout(self):
        """ Once asked to begin monitoring, clients should indicate almost immediately whether they're able to.  But if
            one/both fails to respond, assume they can't monitor.
        """
        if self._closed or not self._emulationStarted:
            return

        for client in self.clients:
            if self._calibrated[client.side] == None:
                self.update(client, evt=MatchEvent.CalibrationFail)

    def _matchStartFail(self, err):
        """ Match startup has failed.  Decide whether to retry or just clean up and quit.

        Args:
            err: MatchError member indicating reason for failure
        """
        if self._startTimeoutTimer != None:
            self._startTimeoutTimer.cancel()

        self._instances.pop(self.quark, None)
        self.quark = None

        if self._startupRetries > 0:
            self._extIO.killEmulator(self.clients[0])
            self._extIO.killEmulator(self.clients[1])

            self._emitEvents([(MatchEvent.StartupFailedRetrying, (err,))])
            self._waitingToRestart = True

            # We can't immediately restart since we need to wait for the client to close the emulator and signal
            # back as ready.  Wait 5 seconds for that to happen before giving up.
            self._retryTimeoutTimer = _DerpTimer(5, self._onRetryTimeout)
            self._retryTimeoutTimer.daemon = True
            self._retryTimeoutTimer.start()

            self._onStatusChanged(self.clients[0])  # try to restart immediately if we can, otherwise wait until players
                                                    # become available
        else:
            self._emitEvents([(MatchEvent.StartupFailedNotRetrying, (err,))])
            self.close(True)

    @synchronized
    def update(self, client, values=None, evt=None, evtparams=()):
        """ Relay information from client regarding match in progress

        Either values or evt, evtparams must be filled

        Args:
            client: sending client
            values: data values read from game memory
            evt: MatchEvent member
            evtparams: additional data for event

        """
        if self._closed or not self._emulationStarted or client.quark != self.quark or self.quark == None:
            return

        side = client.side

        eventsToEmit = []
        if evt != None:
            self._events[side].append((evt, tuple(evtparams)))

            # one of CalibrationFail, CalibrationSuccess, or ResetPressed
            if evt == MatchEvent.CalibrationFail or evt == MatchEvent.CalibrationSuccess:
                if self._calibrated[side] == None:
                    self._calibrated[side] = (evt == MatchEvent.CalibrationSuccess)

                    if side == self._sideUsed and self._calibrated[side] == True:
                        eventsToEmit = [(MatchEvent.CalibrationSuccess, ())]

                    elif (side == self._sideUsed and self._calibrated[side] == False and self._calibrated[3-side] == True) or \
                         (side != self._sideUsed and self._calibrated[side] == True  and self._calibrated[3-side] == False):
                        # change our chosen side if that side cannot monitor but the other can
                        self._sideUsed = 3 - self._sideUsed
                        eventsToEmit = self._events[self._sideUsed]

                    elif self._calibrated[1] == False and self._calibrated[2] == False:
                        eventsToEmit = [(MatchEvent.CalibrationFail, ())]


                    if self._calibrated[self._sideUsed] == True or (self._calibrated[1] == False and self._calibrated[2] == False):
                        self.calibrated = self._calibrated[self._sideUsed]
                        if self._calibrateTimeoutTimer != None:
                            self._calibrateTimeoutTimer.cancel()

            else:
                if side == self._sideUsed:
                    eventsToEmit = [(evt, evtparams)]

        elif values != None and self._updatehandler != None and self._calibrated[side]:
            if self._oldValues[side] != None:  # skip first time through
                newEvents = self._updatehandler(self._oldValues[side], values)
                self._events[side] += newEvents
                if side == self._sideUsed:
                    eventsToEmit = newEvents

            self._oldValues[side] = values

        self._emitEvents(eventsToEmit)

    def _emitEvents(self, events):
        for evt, evtparams in events:
            if evt == MatchEvent.GameStarted:
                self._gameActive = True
            elif evt == MatchEvent.GameEnded:
                self._gameActive = False
                self.roundScore = {1:0, 2:0}
                self.health = {1:100, 2:100}
                self.gameScore[evtparams[0]] += 1
                self._insertSpectators()
            elif evt == MatchEvent.RoundEnded:
                self.roundScore[evtparams[0]] += 1
            elif evt == MatchEvent.EmulationStarted:
                self._insertSpectators()
            elif evt == MatchEvent.HealthChanged:
                self.health = {1:evtparams[0], 2:evtparams[1]}
            elif evt == MatchEvent.TimerChanged:
                self.timer = evtparams[0]

            if self._callback != None:
                self._callback(self, evt, evtparams)
                # DEBUG
                # try:
                #     self._callback(self, evt, evtparams)
                # except Exception as e:
                #     print e
                #     logging.error("Error in callback for match between %s and %s." % (self.clients[0].nick, self.clients[1].nick))

    @synchronized
    def spectate(self, clients):
        """ Queue spectators for this match.  Spectators will be added as soon as possible, depending on the spectator
            policy and whether monitoring is enabled for this match.

        Args:
            clients: list of clients to spectate.

        """
        if self._closed:
            return

        self._queuedSpectators += set(clients)

        if not (self._spectatorPolicy == 1 and self._gameActive == True):
            self._insertSpectators()

    @synchronized
    def unspectate(self, clients):
        """ Remove previously queued spectators.

        Args:
            clients: list of clients previously queued as spectators
        """
        if self._closed:
            return

        self._queuedSpectators -= set(clients)

    def canSpectateNow(self):
        """ Are spectators allowed to join right now via double clicking matches on the player list? """
        return (self._spectatorPolicy == 0 or (self._spectatorPolicy == 1 and self._gameActive == False)) and not self._closed

    def canSpectateEver(self):
        """ Are spectators allowed to join via double clicking matches on the player list *at all*?"""
        return not (self._spectatorPolicy == 2) and not self._closed

    def _insertSpectators(self):
        while True:
            try:
                spec = self._queuedSpectators.pop()
                spec.handle_watch((random.choice(self.clients), -1))
            except KeyError:
                return

    @classmethod
    def matchFromClient(cls, client):
        return cls._instances.get(client.quark)

    def _availability(self):
        # Check if clients are available to play
        return [client in self._ggpochannel.clients and
                client.status != 2 and
                client.quark == None
                for client in self.clients]


    """ Update handlers for individual games.

        Args:
            old: Results of the previous memory scan
            new: Results of the current memory scan

        Returns:
            List of tuples (evt, evtparams)

    """
    # noinspection PyMethodMayBeStatic
    def _analyze_sfiii3n(self, old, new):
        #Frame, oFrame = new[0], old[0]
        Wins, oWins = {1:new[1], 2:new[2]}, {1:old[1], 2:old[2]}
        Rounds, oRounds = {1:new[3], 2:new[4]}, {1:old[3], 2:old[4]}
        Timer_, oTimer_ = new[5], old[5]
        Health, oHealth = {1:new[6], 2:new[7]}, {1:old[6], 2:old[7]}

        events = []

        if (Timer_ > 0 and Health[1] != 255 and Health[2] != 255) and not (oTimer_ > 0 and oHealth[1] != 255 and oHealth[2] != 255):
            # Between/before rounds either the timer will be zero or one player's health will be set to 255.  When this changes,
            # signal that a round is starting
            if Rounds[1] == 0 and Rounds[2] == 0:
                events.append((MatchEvent.GameStarted, None))

            events.append((MatchEvent.RoundStarted, None))

        if Timer_ != oTimer_ and Timer_ < 100:
            events.append((MatchEvent.TimerChanged, (Timer_,)))

        if (Health[1] != oHealth[1] and Health[1] != 255) or (Health[2] != oHealth[2] and Health[2] != 255):
            events.append((MatchEvent.HealthChanged, (int(Health[1]/1.6), int(Health[2]/1.6))))

        if Rounds[1] > oRounds[1]:
            events.append((MatchEvent.RoundEnded, (1,)))

        if Rounds[2] > oRounds[2]:
            events.append((MatchEvent.RoundEnded, (2,)))

        if Wins[1] > oWins[1]:
            events.append((MatchEvent.GameEnded, (1,)))

        if Wins[2] > oWins[2]:
            events.append((MatchEvent.GameEnded, (2,)))

        return events


class _DerpEvent(object):
    """ Crappy remake of Qt signals since python doesn't have publish-subscribe events.

        Works roughly the same, except the first argument to .connect(), .disconnect(), and .emit() must always be the
        associated channel name.  Handlers registered with a specific channel will only receive events emitted in that
        channel.  If None is passed as the channel to .connect(), the handler will be called every time the event fires
        in any room.
    """

    def __init__(self):
        self._handlers = {}

    def connect(self, channel, handler):
        self._handlers.setdefault(channel, set()).add(handler)

    def disconnect(self, channel, handler):
        self._handlers.get(channel, set()).discard(handler)

    def emit(self, channel, *args, **kwargs):
        for handler in self._handlers.get(channel, set()) | self._handlers.get(None, set()):
            handler(*args, **kwargs)



class _DerpTimer(object):
    """ Crappy remake of threading.Timer where every callback runs on a single thread.  Use in place of regular Timers
        to reduce thread spawning.  Patterned after threaing.Timer to make it trivial to switch between them if desired.

        Timer callbacks execute serially, so the timing may not be precise if many long-running methods are scheduled
        to run at the same time.
    """
    _executionThread = None
    _lock = threading.Lock()
    _scheduled = []
    _evtNewItem = threading.Event()

    # noinspection PyDefaultArgument
    def __init__(self, delay, target, args=[], kwargs={}):
        self._obj = [datetime.timedelta(seconds=delay), target, args, kwargs]  # will get added to _scheduled in start()

        if self._executionThread == None:
            with self._lock:
                if self._executionThread == None:
                    self.__class__._executionThread = threading.Thread(target=self._loop)
                    self._executionThread.daemon = True
                    self._executionThread.start()

    def start(self):
        with self._lock:
            self._obj[0] += datetime.datetime.now()
            bisect.insort(self._scheduled, self._obj)  # datetime is first member, so this keeps list sorted by time of execution
            self._evtNewItem.set()

    def cancel(self):
        with self._lock:
            try:
                self._scheduled.remove(self._obj)
            except:
                pass

    @classmethod
    def _loop(cls):
        while True:
            with cls._lock:
                # check queue for something to do

                cls._evtNewItem.clear()

                if len(cls._scheduled) > 0:
                    tm, callback, args, kwargs = cls._scheduled[0]
                    waittime = (tm - datetime.datetime.now()).total_seconds()
                    if waittime <= 0:
                        cls._scheduled.pop(0)  # remove from queue if we're going to run it
                else:
                    waittime = None

            if waittime == None:  # nothing in the list, wait for something to be added
                cls._evtNewItem.wait()

            elif waittime <= 0:  # first item in the list is ready to execute
                try:
                    # noinspection PyUnboundLocalVariable
                    callback(*args, **kwargs)
                except:
                    pass

            else:  # next item can't execute yet.  Wait for it to be ready or for something else to be added
                cls._evtNewItem.wait(waittime)


# noinspection PyClassHasNoInit
class _SpecStrings:
    spectatingRestricted = "Joining as a spectator while a game is in progress is disabled for this match. " +\
                           "Click %s to automatically spectate when the current game ends."
    spectatingForbidden = "Spectating not allowed for this match"
    autospectateOK = "You will automatically spectate this match when the current game ends.  " + \
                     "Click %s to cancel."
    autospectateRemoved = "Auto-spectate canceled."
    autospectateError = "Error adding you to auto-spectate queue.  Probably the match has already ended."

# noinspection PyClassHasNoInit
class _Message:
    # client to server
    InitializationDataRequest = 1
    ExtensionNotFound = 2
    AnchorClicked = 3
    ChatCommand = 4
    MatchEvent = 5
    MatchData = 6

    # server to client
    InitializationDataResponse = 101
    ChatMessage = 102
    KillEmulator = 103
    BeginMonitoring = 104
