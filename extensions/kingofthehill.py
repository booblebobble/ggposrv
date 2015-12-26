from extensionbase import *

# includes current king
QUEUE_MAX_SIZE = 4


class KingOfTheHill(ExtensionBase):

    playerColorRGB = (128, 255, 0) # light green
    spectatorPolicy = 2  # Users spectate by using the Spectate button

    def __init__(self, *args, **kwargs):
        ExtensionBase.__init__(self, *args, **kwargs)

        self.ChannelLeaving.connect(self.channel, self.onChannelLeaving)

        self.updateRecipients = set()  # clients who have panel open.
        self.spectators = set()  # clients to automatically add as spectators when matches start
        self.queue = []  # clients in queue.  First element is the client of the current winner.
        self.streak = 0  # number of matches the current winner has won in a row.

        self.currentMatch = None

    @synchronized
    def receiveMessage(self, client, prefix, params):
        if prefix == Message.UpdateRequest:
            # Client wants queue info resent
            self.sendMessage(client, Message.UpdateQueue, self._queueInfo())

        elif prefix == Message.UpdatesOn:
            # User opened KOTH foldout.  Put them on the list for live game updates
            self.updateRecipients.add(client)
            self.sendMessage(client, Message.UpdateQueue, self._queueInfo())

        elif prefix == Message.UpdatesOff:
            # User closed KOTH foldout.  Stop sending them updates.
            self.updateRecipients.discard(client)
            self._removeFromQueue(client)

        elif prefix == Message.SpectateOn:
            # Add user to spectator list.  They will automatically spectate all future KOTH matches until further notice
            self.spectators.add(client)

        elif prefix == Message.SpectateOff:
            # Remove user from spectator list.
            self.spectators.discard(client)

        elif prefix == Message.JoinQueueRequest:
            # User wants to join
            self._addToQueue(client)

        elif prefix == Message.LeaveQueue:
            # Queued player leaving
            self._removeFromQueue(client)

    @synchronized
    def onChannelLeaving(self, client):
        self.updateRecipients.discard(client)
        self.spectators.discard(client)
        if client in self.queue:
            self._removeFromQueue(client)

    @synchronized
    def matchCallback(self, match, event, eventparams):
        """ callback for ExtensionMatch object """

        if match != self.currentMatch:
            # why are you talking to me? (shouldn't happen)
            return

        if event == MatchEvent.CalibrationFail or event == MatchEvent.StartupFailedNotRetrying:
            if self.queue[0].os == 1:
                winner = 1
            elif self.queue[1].os == 1:
                winner = 2
            else:
                winner = 1 # Shouldn't happen.  _beginMatch will catch.

            self.chatMessage(self.queue[2-winner], "Match startup or initialization failed. You have been" +
                                                   " removed from the queue.  Sorry about that.")
            self._endMatch(winner, False)

        elif event == MatchEvent.StartupFailedRetrying:
            self.broadcastChatMessage("Match failed to load.  Retrying once.  If game fails to load again, the current " +
                                      "match will be skipped.")

        elif event == MatchEvent.ResetPressed:
            self.chatMessage(self.queue[0], "Resetting the game is not allowed.  You have been DQ'd.")
            self._endMatch(2)

        elif event == MatchEvent.GameEnded:
            self._endMatch(eventparams[0])

        elif event == MatchEvent.EmulatorClosed:
            self._endMatch(3-eventparams[0])

    def _addToQueue(self, client):
        """ Try to add player to queue """
        if client in self.queue:
            # lolwut?
            self.sendMessage(client, Message.UpdateQueue, self._queueInfo())

        elif len(self.queue) >= QUEUE_MAX_SIZE:
            self.sendMessage(client, Message.JoinRequestDeniedQueueFull)

        else:
            self.queue.append(client)
            self.broadcastMessage(Message.PlayerJoined, (client.nick, client.os), clients=self.updateRecipients)

            # if he was the second player to join, start a match
            if len(self.queue) == 2:
                self._beginMatch()

    def _removeFromQueue(self, client):
        """  Take a client out of the queue, canceling his match in progress if necessary. """
        if client in self.queue:
            if client in self.queue[:2] and self.currentMatch != None:
                self._endMatch(2 if client==self.queue[0] else 1, updateStreak=False)
            else:
                self.broadcastMessage(Message.PlayerLeft, client.nick, clients=self.updateRecipients)
        else:
            # lolwut?
            self.sendMessage(client, Message.UpdateQueue, self._queueInfo())

    def _queueInfo(self):
        # noinspection PyTypeChecker
        return [self.streak] + [(cl.nick, cl.os) for cl in self.queue]

    def _beginMatch(self):
        """ Begin match between the first two players in the queue """

        if len(self.queue) < 2 or self.currentMatch == None:
            return # shouldn't happen.

        if self.queue[0].os != 1 and self.queue[1].os != 1:
            self.chatMessage(self.queue[1], "Since neither you nor the current king are using Windows, the match " +
                                            "cannot be played, and you have been removed from the queue.  Sorry.")
            self._removeFromQueue(self.queue[1]) #_removeFromQueue will call _endMatch which will re-call _beginMatch
            return

        self.broadcastChatMessage("Match starting.  If game does not load within 30 seconds, it will automatically retry.  " +
                                  "Please do not close the game window or try to challenge each other.")

        try:
            self.currentMatch = self.createMatch(self.queue[:2], self.matchCallback, spectators=self.spectators)
        except MatchError as e:
            if e.code == MatchErrorCode.P2Unavailable or e.code == MatchErrorCode.BothPlayersUnavailable:
                self.chatMessage(self.queue[1], "You were not available for your match.")
                self.queue.remove(1)

            if e.code == MatchErrorCode.P1Unavailable or e.code == MatchErrorCode.BothPlayersUnavailable:
                self.chatMessage(self.queue[0], "You were not available for your match.")
                self.queue.remove(0)
                self.streak = 0

            self.broadcastMessage(Message.UpdateQueue, self._queueInfo(), clients=self.updateRecipients)

            if len(self.queue) >= 2:
                self._beginMatch()

    def _endMatch(self, winner, updateStreak=True):
        """ Match is ending.  Remove loser from queue and start next match.

        Args:
            winner: Side of winner, 1 or 2
            updateStreak: If False, don't count result toward winner's streak
        """
        self.streak = (0 if winner == 2 else self.streak) + (1 if updateStreak else 0)

        self.broadcastMessage(Message.PlayerLeft, self.queue[2-winner].nick, clients=self.updateRecipients)
        self.broadcastMessage(Message.StreakUpdate, self.streak, clients=self.updateRecipients)

        del self.queue[2-winner]

        self.currentMatch.close()
        self.currentMatch = None

        if len(self.queue) >= 2:
            self._beginMatch()


# noinspection PyClassHasNoInit
class Message:
    #server to client
    UpdateQueue = 1
    UpdateGameStatus = 2
    PlayerJoined = 3
    PlayerLeft = 4
    StreakUpdate = 5
    JoinRequestDeniedQueueFull = 6
    JoinRequestDeniedPleaseWait = 7

    #client to server
    JoinQueueRequest = 101
    LeaveQueue = 102
    UpdateRequest = 103
    UpdatesOff = 104
    UpdatesOn = 105
    SpectateOff = 106
    SpectateOn = 107
