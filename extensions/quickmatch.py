""" Basic matchmaking.  Complicated slightly by the fact that only the client knows his ping with other users.

- Users click a button to be added to the pool for the room
- Server keeps pool members updated about who else is in the pool
- Client tells server which players from the pool have acceptable ping
- If two players both satisfy each other's ping requirements, start a match between them
- If possible, kill the emulator after one game
"""
from extensionbase import *
from itertools import ifilter

class QuickMatch(ExtensionBase):
    def __init__(self, *args, **kwargs):
        ExtensionBase.__init__(self, *args, **kwargs)

        self.ChannelLeaving.connect(self.channel, self.onChannelLeaving)
        self.StatusChanged.connect(self.channel, self.onStatusChanged)

        self.searchingPlayers = {}  # search pool.  nick => GGPOClient instance
        self.proposedMatches = set()  # set of tuples (client1, client2), indicating client1 wants to fight client2

    @synchronized
    def receiveMessage(self, client, prefix, params):
        if prefix == Message.StartSearch:
            # join search pool
            self.broadcastMessage(Message.SearchPoolAdd, [client.nick], clients=self.searchingPlayers.values())
            self.sendMessage(client, Message.SearchPoolAdd, self.searchingPlayers.keys())
            self.searchingPlayers[client.nick] = client

        elif prefix == Message.CancelSearch:
            # leave search pool
            self.searchingPlayers.pop(client.nick, None)
            self.broadcastMessage(Message.SearchPoolRemove, [client.nick], clients=self.searchingPlayers.values())
            self.proposedMatches = set(ifilter(lambda match: client not in match, self.proposedMatches))

        elif prefix == Message.ResetRequests:
            # user has modified his acceptable ping, so cancel all their pending match requests.
            # client will resend requests for opponents meeting his new ping setting
            self.proposedMatches = {(p1, p2) for (p1, p2) in self.proposedMatches if p1 != client}

        elif prefix == Message.RequestMatch:
            # client wants to fight player whose username is in params.
            if client not in self.searchingPlayers.values():
                return

            oppclients = {self.searchingPlayers.get(name) for name in params} - {None}
            for oppclient in oppclients:
                if (oppclient, client) in self.proposedMatches:
                    # other player has already requested a fight with client, so start match.
                    self.searchingPlayers.pop(client.nick, None)
                    self.searchingPlayers.pop(oppclient.nick, None)
                    self.proposedMatches = {m for m in self.proposedMatches if client not in m and oppclient not in m}

                    self.broadcastMessage(Message.SearchPoolRemove, [client.nick, oppclient.nick], clients=self.searchingPlayers.values())
                    self.broadcastMessage(Message.MatchStarting, clients=[oppclient, client])

                    try:
                        self.createMatch([client, oppclient], self.matchCallback, randomizeSides=True)
                    except:
                        pass

                    break
                else:
                    self.proposedMatches.add((client, oppclient))

    @synchronized
    def matchCallback(self, match, evt, eventparams):
        if evt == MatchEvent.GameEnded:
            if match.score[1] >= 2 or match.score[2] >= 2:
                match.close(delay=3)

                self.sendMessage(match.clients[1], Message.MatchEnding, match.score[1] >= 2)
                self.sendMessage(match.clients[2], Message.MatchEnding, match.score[2] >= 2)

    @synchronized
    def onStatusChanged(self, client):
        if client.status != 0:
            self._discardSearchingPlayer(client)

    @synchronized
    def onChannelLeaving(self, client):
        self._discardSearchingPlayer(client)

    def _discardSearchingPlayer(self, client):
        if client in self.searchingPlayers.values():
            self.searchingPlayers.pop(client.nick, None)
            self.proposedMatches = [m for m in self.proposedMatches if client not in m]
            self.broadcastMessage(Message.SearchPoolRemove, [client.nick], clients=self.searchingPlayers.values())


# noinspection PyClassHasNoInit
class Message:
    # server to client
    SearchPoolAdd = 1
    SearchPoolRemove = 2
    MatchStarting = 3
    MatchEnding = 4

    # client to server
    RequestMatch = 101
    StartSearch = 102
    CancelSearch = 103
    ResetRequests = 104
