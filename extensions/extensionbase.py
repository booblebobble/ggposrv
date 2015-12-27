from functools import partial
from itertools import ifilter
from extensionmatchconstants import *
from wrapt.decorators import synchronized  # decorator that mimics java 'synchronized' keyword.
                                           # https://github.com/GrahamDumpleton/wrapt/blob/develop/docs/examples.rst

class ExtensionBase(object):
    """Base class for extension server component

    This class is the base class for all extensions. An extension module must contain one main class deriving from
    ExtensionBase.  This class will be instantitated once for each channel on server startup.

    ExtensionBase provides access to application events (Qt signals) that may be hooked into to receive notification
    about application activities such as the user changing rooms or other players changing their status.  Access to UI
    elements can be done through the cls.ggpowindow member, which contains a handle to the main application window, and
    chatMessage can be used to display text strings in the chat box.

    The methods sendMessage and receiveMessage are used to communicate with the server component of the extension.

    """

    # A match which has been subscribed to by this extension will appear as this color in the player list.
    # Override with a 3-tuple of integers (i.e. (255, 0, 0) for red) or None to use the default
    playerColorRGB = None

    # Override with one of the following to set spectating policy for this extension
    #    0 - Normal spectating, no limitations.
    #    1 - Spectators are blocked from joining during games but are given the option to automatically spectate
    #        after the current game finishes.  This only applies to matches that can be monitored in full detail.  For
    #        other matches, spectating is allowed as normal.
    #    2 - Spectating attempts are always blocked.
    spectatorPolicy = 0

    # Override this with a keyword for chat commands, if needed.  Setting this to a string 'foo' will cause user-entered
    # commands like "/foo a b c" to be routed to receiveChatCommand
    chatCommand = None

    instances = {}      # dictionary of all instances of the extension (derived class, not ExtensionBase), keyed by channel name.
    admins = []         # list of users singled out as administrators.  (probably just pof)
    ggposerver = None   # handle of the main GGPOServer instance
    extID = -1          # id number of this extension (from _ExtensionDict in extension.py)
    __extIO = None

    @classmethod
    def Initialize(cls, extID, admins, extensionIO):
        """ Called before instantiation to store various things.

            Don't override.
        """

        cls.extID = extID
        cls.admins = admins
        cls.__extIO = extensionIO

        cls.ggposerver = cls.__extIO.ggposerver

#        cls.Logon = cls.__extIO.Logon
#        cls.Logoff = cls.__extIO.Logoff
        cls.ChannelJoin = cls.__extIO.ChannelJoin
        cls.ChannelLeaving = cls.__extIO.ChannelLeaving
        cls.EmulatorClosed = cls.__extIO.EmulatorClosed
        cls.MatchStarting = cls.__extIO.MatchStarting
        cls.StatusChanged = cls.__extIO.StatusChanged
#        cls.ChallengeAccepted = cls.__extIO.ChallengeAccepted
#        cls.EmulatorOpened = cls.__extIO.EmulatorOpened

        # RGB values use 3 bytes, so use the 4th byte as a flag to indicate if we actually have a custom color
        # or should leave it as the default
        if cls.playerColorRGB == None:
            cls.playerColorRGB = 0x1000000
        else:
            cls.playerColorRGB = (cls.playerColorRGB[0] << 16) + (cls.playerColorRGB[1] << 8) + cls.playerColorRGB[2]

    def __init__(self, ggpochannel):
        self.ggpochannel = ggpochannel
        self.channel = ggpochannel.name

    @classmethod
    def setInstances(cls, instances):
        """
        Store dictionary of extension instances.  Used by Extension

        Args:
            instances: dict mapping channel name to extension instance

        """
        cls.instances = instances

    def setPlayerColor(self, username, color):
        """ Set the color a player will appear as in client player lists.

        Args:
            username: name of player to set color for
            color: RGB value encoded as a 3-byte integer, or None for default

        """
        self.__extIO.setPlayerColor(self.channel, username, color)

    @classmethod
    def onInstantiationEnded(cls):
        """ Override if needed.  Called after all room instances have been created for this extension """
        pass

    def monitoringAvailable(self):
        """Returns True or False according to whether event monitoring is available for our channel

        Does not gurantee that monitoring will be available for every match in this channel.
        """
        return self.__extIO.monitoringAvailable(self.channel)

    def advancedMonitoringAvailableMatch(self, id_):
        """ If an extension subscribes to a match while it is already in progress, the initial EmulatorOpened and
            CalibrationSuccess/CalibrationFail events will not be sent.  This method can be used to test whether game
            event monitoring is available for this matchup

        Args:
            id_: ID number returned by subscribe()

        Returns:
            None: Match is not currently active or calibration hasn't yet succeeded or failed
            True/False: Match is active and monitoring is/isn't available

        """
        return self.__extIO.referee.advancedMonitoringAvailableMatch(self, id_)

    def readUserData(self, name=None, channel=0):
        """Retrieves stored object from extension database associated with a username and channel

        Args:
            name: A username.  String or None.
            channel: A channel identifier.  String or None.  Defaults to the room associated with this instance

        Returns:
            Object previously stored with writeUserData, or None if not found.

        """
        return self.__extIO.readUserData(extID=self.extID, name=name, channel=self.channel if channel==0 else channel)

    def writeUserData(self, name=None, data=None, channel=0):  # use channel=0 to indicate default since we want None to be a usable value
        """Stores the passed object in the extension database

        Args:
            name: A username.  String or None.
            data: A json-serializable python object.
            channel: A channel identifier.  String or None.  Defaults to the room associated with this instance.
        """
        self.__extIO.writeUserData(extID=self.extID, name=name, data=data, channel=self.channel if channel==0 else channel)

    def sendMessage(self, client, prefix, params=None, overrideChannelCheck=False):
        """Sends an extension message to the specified client in our room.

        The client instance is verified to be not None and to be in our room.  To send messages to clients in other rooms,
        set the overrideChannelCheck parameter to True.

        The message will be routed to the receiveMessage method in the main class (ExtensionBase subclass) in the client
        module for this extension.

        Args:
            client: A GGPOClient instance.
            prefix: A extension-defined positive integer.
            params: A json-serializable python object.
            overrideChannelCheck: Set to True if sending a message to a client not in our room.

        """
        if client != None and (client.channel.name == self.channel or overrideChannelCheck):
            self.__extIO.sendMessage(self.extID, client, prefix, params)

    def chatMessage(self, client, msg, overrideChannelCheck=False):
        """Displays a string in the chat box of the specified client.

        The client instance is verified to be not None and to be in our room.  To send messages to clients in other rooms,
        set the overrideChannelCheck parameter to True.

        The string will be passed to the GGPOWindow.sppendChat method in the specified client.

        Args:
            client: A GGPOClient instance
            msg: A string.
            overrideChannelCheck: Set to True if sending a message to a client not in our room.
        """
        if client == None or (not overrideChannelCheck and client.channel.name != self.channel):
            return

        self.__extIO.chatMessage(client, msg)

    def createMatch(self, clients, callback, randomizeSides=False, startupTimeout=0, startupRetries=0, spectators=set()):
        """ Starts a match between two users and registers a callback to receive match events.

        Args:
            See definition of ExtensionMatch

        Returns:
            ExtensionMatch object
        """
        return self.__extIO.createMatch(self.extID, self.channel, clients, callback, randomizeSides, startupTimeout, startupRetries, spectators)

    # noinspection PyDefaultArgument
    @classmethod
    def Timer(cls, delay, target, args=[], kwargs={}):
        """ Returns timer-like object whose sole advantage is that it doesn't live in its own thread.  Use instead of
            regular timers if you need a bunch of timers but don't want to create a lot of threads.

            Perfect timing accuracy not guaranteed

        Args:
            delay: same as threading.Timer
            target: same as threading.Timer
            args: same as threading.Timer
            kwargs: same as threading.Timer

        Returns:
            _DerpTimer instance
        """
        return cls.__extIO.Timer(delay, target, args, kwargs)

    def broadcastMessage(self, prefix, params=None, clients=None, names=None, overrideChannelCheck=False):
        """Sends a message to multiple users.

        Specify one or neither of clients, names.

        If clients is specified, the message will be sent to every client in the list in the room.  Otherwise if names is
        specified, the message will be sent to every name in the list who is in the room. If neither is specified, send
        to every user in the channel.

        To send to users outside the channel, set the overrideChannelCheck parameter to True.

        Args:
            prefix: A extension-defined positive integer.
            params: A json-serializable python object.
            clients: Optional iterable of GGPOClient instances
            names: Optional iterable of usernames (strings).
            overrideChannelCheck: Set to True to allow sending messages outside our room.
        """
        tmp = partial(self.__extIO.sendMessage, extID=self.extID, prefix=prefix, params=params)
        self.__broadcast(tmp, clients, names, overrideChannelCheck)

    def broadcastChatMessage(self, msg, clients=None, names=None, overrideChannelCheck=False):
        """Sends a chat message to multiple users.

        Specify one or neither of clients, names.

        If clients is specified, the message will be sent to every client in the list in the room.  Otherwise if names is
        specified, the message will be sent to every name in the list who is in the room. If neither is specified, send
        to every user in the channel.

        To send to users outside the channel, set the overrideChannelCheck parameter to True.

        Args:
            msg: A string.
            clients: Optional iterable of GGPOClient instances
            names: Optional iterable of usernames (strings).
            overrideChannelCheck: Set to True to allow sending messages outside our room.
        """
        tmp = partial(self.__extIO.chatMessage, msg=msg)
        self.__broadcast(tmp, clients, names, overrideChannelCheck)

    def __broadcast(self, func, clients, names, overrideChannelCheck):
        """Helper for broadcastMessage and broadcastChatMessage"""

        if clients == None:
            if names == None:
                clients = list(self.ggpochannel.clients) if not overrideChannelCheck else self.ggposerver.clients.values()
            else:
                clients = map(self.clientFromName, names) if not overrideChannelCheck else map(self.ggposerver.clients.get, names)
        else:
            clients = list(clients)

        pred = lambda cl: cl != None and cl.clienttype == "client" and (cl.channel.name == self.channel or overrideChannelCheck)

        for client in ifilter(pred, clients):
            func(client=client)

    def clientFromName(self, name, channel=0):
        """Returns the GGPOClient instance associated with a name

        By default, return a client only if the user is in our room.  To search another room, set the channel parameter
        to a channel name.  To search every room, set channel to None.

        Args:
            name: User name
            channel: Channel name

        Returns:
            A GGPOClient instance, or None, by the rules above.

        """
        client = self.ggposerver.clients.get(name)

        if client != None and (channel == None or client.channel.name == (self.channel if channel==0 else channel)):
            return client
        else:
            return None

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

    def receiveMessage(self, client, prefix, params):
        """Process a message sent to us by the server half of the extension

        Override if needed.

        NOTE: The second parameter has been json packed and unpacked en route, which may change the object in certain
              circumstances.  For example, tuples will arrive as lists, and dictionaries will arrive with all keys
              converted to strings.

        Args:
            client: GGPOClient instance of the user who sent the message
            prefix: Extension-defined positive integer
            params: Python object
        """
        pass

    # noinspection PyMethodMayBeStatic
    def receiveAnchorClick(self, client, key):
        """Handle an anchor click.

        Override if needed.

        To create a clickable link, use buildAnchorString to generate an html link and include it in a message string
        passed to chatMessage or broadcastChatMessage.

        Args:
            client: GGPOClient instance of the sending client.
            key: Extension-defined string previously passed to buildAnchorString
        """
        pass

    def receiveChatCommand(self, client, args):
        """Handle a chat command.

        Override if needed.

        To use chat commands, override the chatCommand class property.  For instance, setting chatCommand to 'foo' will
        cause user-entered commands like "/foo a b c" to be received here.

        Args:
            client: GGPOClient instance of the sending client.
            args: string containing all text after the command. i.e. "a b c" in the example above.
        """
        pass


