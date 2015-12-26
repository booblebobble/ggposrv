from extensionbase import *
import sys
import os
import logging
import threading
import math
import datetime
import string
import time
import random
import bisect
import challonge
from challonge import ChallongeException
from dateutil.parser import *
from dateutil.tz import *
from urllib2 import HTTPError
from itertools import ifilter

MINIMUM_PARTICIPANTS = 2  # minimum number of participants that must register for a tournament to run
# REGISTRATION_TIME = 30*60       # length of registration period
REGISTRATION_TIME = 30
#READY_TIMEOUT = 5 * 60  # allowed time before an available match must be started
READY_TIMEOUT = 20
#MATCH_FINISH_TIMEOUT = 20 * 60  # allowed time after a match starts before a score must be reported
MATCH_FINISH_TIMEOUT = 60  # allowed time after a match starts before a score must be reported

EMPTY_RESULT = {1: None, 2: None}  # a 'null' score report.

SCHEDULE_PATH = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])), 'tournamentsettings.txt')

class Tournaments(ExtensionBase):
    """ Handles tournament scheduling.
            - reads tournamentsetting.txt file on server startup and on demand if modified
            - creates Bracket object when a tournament is set to begin.  The Bracket object will handle actual execution
            - records tournament winners
    """

    playerColorRGB = (0, 0, 255)  # blue
    spectatorPolicy = 1
    chatCommand = "tournaments"

    def __init__(self, *args, **kwargs):
        ExtensionBase.__init__(self, *args, **kwargs)

        self.schedule = []  # set of TournamentData objects containing info about future tournaments.
        self.running = None  # handle to the Bracket object managing the currently active tournament (if any)

        self.roomChamp = self.readUserData(name=None)  # winner of the last tournament held in this room
        if self.roomChamp != None:
            self.setPlayerColor(self.roomChamp, 0xFFD700)  # 0xFFD700 = gold

        self.ChannelJoin.connect(self.channel, self.onChannelJoin)
        self.ChannelLeaving.connect(self.channel, self.onChannelLeaving)
        self.StatusChanged.connect(self.channel, self.onStatusChanged)
        self.MatchStarting.connect(self.channel, self.onMatchStarting)

    @classmethod
    def onInstantiationEnded(cls):
        cls._loadSchedule()

    @synchronized
    def receiveMessage(self, client, prefix, params):
        if self.running != None:
            self.running.receiveMessage(client, prefix, params)

    @synchronized
    def onChannelJoin(self, client):
        if self.running != None:
            self.running.onChannelJoin(client)

    @synchronized
    def receiveChatCommand(self, client, args):
        if len(args) > 0 and args == "reload" and client.nick in self.admins:
            self._loadSchedule(client)
        else:
            self.chatMessage(client, "Command not recognized")

    @synchronized
    def onChannelLeaving(self, client):
        if self.running != None:
            self.running.onChannelLeaving(client)

    @synchronized
    def onStatusChanged(self, client):
        if self.running != None:
            self.running.onStatusChanged(client)

    @synchronized
    def onMatchStarting(self, quark):
        if self.running != None:
            self.running.onMatchStarting(quark)

    @synchronized
    def startTournament(self, tmdata):
        """ A scheduled tournament is due to start.  Verify it's OK to begin and create bracket."""
        if tmdata not in self.schedule:
            return

        if self.running != None:
            logging.error("Tournament Error: Tournament %s could not start because %s is still active." % (tmdata.name, self.running.name))
        else:
            self.running = tmdata.createBracket()
            if tmdata.period == 0:
                self.schedule.remove(tmdata)

    def reportEnding(self, bracket):
        """ Bracket signals tournament is ending. Record winner."""
        if self.running != bracket:
            return
        else:
            self.running = None
            if bracket.winner != None:
                self.writeUserData(name=None, data=bracket.winner)
                self.roomChamp = bracket.winner
                self.setPlayerColor(bracket.winner, 0xFFD700)  # 0xFFD700 = gold

    @classmethod
    def scheduledTournaments(cls, channel, client=None):
        """ Return a list of scheduled tournaments in this channel.

            Args:
                channel: channel name
                client: GGPOclient instance

            Returns:
                A possibly empty list of tuples (name, startTime) giving the tournament name and its start time
                in the client's local time zone.

                A currently running tournament is included only if registration is still in progress
        """
        instance = cls.instances.get(channel)
        if instance == None:
            return

        tz = tzoffset(None, client.utcoffset)
        print tz

        res = []
        try:
            if instance.running != None and instance.running.state == BracketState.Registration:
                res.append((instance.running.name, instance.running.startTime.astimezone(tz)))
        except:
            pass

        res += [(td.name, td.startTime.astimezone(tz)) for td in list(instance.schedule)]

        print res

        return res

    @classmethod
    def _loadSchedule(cls, client=None):
        """ Read in the schedule from the tournamentsettings.txt file.
                client - the (admin) client that initiated the reload, or None if running at server startup.
        """

        # Two types of lines we need to look at: "X=Y" and "[title]"
        try:
            with open(SCHEDULE_PATH, "r") as scheduleFile:
                lines = []
                for line in scheduleFile:
                    line = line.strip()
                    if line != "" and (line.find("=") != -1 or (line[0] == "[" and line[-1] == "]")):
                        lines.append(line)
        except Exception as e:
            cls._errorMessage(client, "Error (%s) parsing tournament schedule (db/tournamentsettings.txt)" % repr(e))
            return

        # Collect parameter assignments for each tournament into dicts, then put those dicts into a bigger dict keyed by tm name
        tournamentsRaw = {}
        name = None  # for parameters encountered before first title
        for line in lines:
            if line[0] == "[" and line[-1] == "]":
                name = line[1:-1]
                if name in tournamentsRaw:
                    cls._errorMessage(client,
                                      ("Error reading tournament schedule: Duplicate tournament title %s. "
                                       "Please correct and reload.") % name)
                    return
            else:
                key, val = line.split("=", 1)
                tournamentsRaw.setdefault(name, {}).setdefault(key, []).append(val)

        # Retreive login credentials and check that they work
        header = tournamentsRaw.pop(None, {})

        if "username" in header and "apikey" in header:
            challonge.set_credentials(header["username"][-1], header["apikey"][-1])

            thd = threading.Thread(target=cls._verifyCredentials, args=(client,))
            thd.dameon = True
            thd.start()
        else:
            if len(tournamentsRaw) > 0:
                cls._errorMessage(client,
                                  "Error reading tournament schedule: Challonge username/apikey not found.  No tournaments will run.")
                return

        tournamentsParsed = {}
        errmsg = "Parse error for tournament %s: %s.  Please correct and reload schedule"

        for name, opts in tournamentsRaw.iteritems():
            channel = opts.get("channel", [None])[-1]
            mods = cls.admins + opts.get("moderator", [])
            periodStr = opts.get("period", ["0"])[-1]
            startTimeStr = opts.get("starttime", [None])[-1]
            rules = "\r\n".join(opts.get("rules", ["Selur Eht Era Selur"]))

            if channel == None:
                cls._errorMessage(client, errmsg % (name, "Channel not specified"))
                return

            if channel not in cls.instances:
                cls._errorMessage(client, errmsg % (name, "Invalid channel"))
                return

            try:
                period = int(periodStr)
            except:
                cls._errorMessage(client, errmsg % (name, "Period is non-numeric"))
                return

            if startTimeStr == None:
                cls._errorMessage(client, errmsg % (name, "Start time not given"))
                return

            try:
                startTime = parse(startTimeStr, dayfirst=True, yearfirst=True)
                startTime = startTime.replace(tzinfo=startTime.tzinfo or tzutc())  # interpret time as UTC unless other timezone given

                # DEBUG
                startTime = datetime.datetime.now(tzlocal()) + datetime.timedelta(seconds=REGISTRATION_TIME)
            except:
                cls._errorMessage(client, errmsg % (name, "Could not parse start time"))
                return

            registrationStartTime = startTime - datetime.timedelta(seconds=REGISTRATION_TIME)
            if registrationStartTime < datetime.datetime.now(tzlocal()):
                # start time given in file has already passed.

                if period == 0:
                    # For one-time tournaments, just skip
                    cls._errorMessage(client, "Warning: Registration start time for one-time tournament %s has passed.  Tournament not scheduled" % (name,))
                    continue
                else:
                    # change start time of periodic tournament to the next available date.

                    # number of days we must bump start time ahead by
                    offset = period * math.ceil((datetime.datetime.now(tzlocal()) - registrationStartTime).total_seconds() / (24*60*60*period))
                    startTime = startTime + datetime.timedelta(days=offset)

            tournamentsParsed.setdefault(channel, []).append({"channel":channel,
                                                              "name":name,
                                                              "mods":mods,
                                                              "rules":rules,
                                                              "startTime":startTime,
                                                              "period":period})

        cls._errorMessage(client, "Tournament schedule file read successfully.  Updating schedule.", actualError=False)

        for instance in cls.instances.values():
            instance.reloadSchedule(client, tournamentsParsed.get(instance.channel, []))


    @synchronized
    def reloadSchedule(self, client, newTournaments):
        """ Schedule is being loaded/reloaded.  Cancel all future tournaments and schedule the ones in newTournaments """
        for td in self.schedule:
            self._errorMessage(client, "Canceling tournament %s in room %s." % (td.name, td.channel), actualError=False)
            td.cancel()

        self.schedule = []
        for tmdata in newTournaments:
            td = TournamentData(self, **tmdata)
            bisect.insort(self.schedule, td)
            self._errorMessage(client, "Tournament %s scheduled for %s in room %s." % (td.name, td.startTime, td.channel), actualError=False)


    @classmethod
    def _verifyCredentials(cls, client):
        """ Checks that the username/API key from the tournamentsettings.txt is valid """
        try:
            # dummy request with predictable response merely to check that we are authorized to make requests.
            # noinspection PySimplifyBooleanCheck
            if challonge.tournaments.index(created_after="2099-01-01") != []:
                raise Exception("Either challonge derped or time travelers are screwing with us.")
            cls._errorMessage(client, "Challonge credentials verified.", actualError=False)
        except Exception as e:
            # noinspection PyUnresolvedReferences
            if isinstance(e, HTTPError) and e.code == 401:
                cls._errorMessage(client,"ERROR: Challonge credentials rejected.  Tournaments will fail to start unless corrected.")
            else:
                cls._errorMessage(client,
                                  "WARNING: Challonge credential verification check failed with unexpected exception " + repr(e) + \
                                  ". Tournaments will fail if credentials are invalid.")

    @classmethod
    def _errorMessage(cls, client, msg, actualError=True):
        # route errors encountered during schedule loading to the requesting client, unless done during server startup
        if actualError:
            logging.error(msg)
        else:
            logging.info(msg)

        if client != None:
            try:
                cls.instances[client.channel.name].chatMessage(client, msg, overrideChannelCheck=True)
            except:
                pass


class TournamentData:
    """ Store data for a future tournament and notify manager when it is time to start. """

    def __init__(self, manager, name, channel, mods, rules, startTime, period):
        self.manager = manager
        self.channel = channel
        self.name = name
        self.mods = mods
        self.rules = rules
        self.startTime = startTime
        self.period = period
        self._startTimer = None

        self._setTimer()

    def __lt__(self, other):
        return self.startTime < other.startTime

    def cancel(self):
        self._startTimer.cancel()

    def createBracket(self):
        """ Manager has determined we can start.  Create a Bracket object to execute the tournament. """
        return Bracket(manager=self.manager,
                       name=self.name,
                       channel=self.channel,
                       mods=self.mods,
                       rules=self.rules,
                       startTime=self.startTime)

    def _setTimer(self):
        # calculate time until registration starts
        waittime = (self.startTime - datetime.timedelta(seconds=REGISTRATION_TIME) - datetime.datetime.now(tzlocal())).total_seconds()
        self._startTimer = self.manager.Timer(waittime, self._tryStart)
        self._startTimer.daemon = True
        self._startTimer.start()

    def _tryStart(self):
        """ It's time to start.  Notify the manager, who will call createBracket if we actually should start.  Then prepare
            for the next run if we are a recurring tournament.
        """
        self.manager.startTournament(self)

        if self.period != 0:
            self.startTime += datetime.timedelta(days=self.period)
            self._setTimer()


# noinspection PyClassHasNoInit
class BracketState:
    Registration, Open, Suspended, Completed = range(4)


class Bracket(object):
    """ Creates a challonge bracket for a scheduled tournament and oversees its execution from start to finish.
            - Handles registration requests and score reports and relays them to challonge
            - Loads bracket constructed by challonge and translates it into a set of interlocked Match objects which
              handle the tournament flow
            - Keeps clients updated about their current position in the tournament
            - Communicates with moderators for conflict resolutions.
            - On tournament completion, notifies the owning Tournaments instance of the winner.

            Once registration is over and matches start, the challonge bracket is purely cosmetic, and the tournament can proceed
            even if challonge goes down or something, though users won't be able to watch tournament progress.
    """
    def __init__(self, manager, name, mods, channel, startTime, rules):
        self.manager = manager  # Tournaments instance that owns us. Needed for outgoing messages and to report back when the tournament ends.
        self.name = name  # Title of the tournament
        self.moderators = mods  # Moderators can submit score reports for any open match.
        self.channel = channel  # Channel tournament is running in
        self.startTime = startTime  # datetime object storing time matches will start (not registration start time, which is right now)
        self.rules = rules  # Rules for this tournament, to display to players

        self.winner = None  # Name of winner
        self.urltail = None  # ID string to identify our tournament in challonge API calls.  Bracket url will be challonge.com/<urltail>
        self.challongeData = None  # Data received from challonge on tournament creation
        self.participants = {}  # ID number => username
        self.pendingRegistrations = set()  # Users with registration/unregistration requests currently being submitted to challonge.
#        self.kickedPlayers = set()  # Players who've been kicked by a moderator.
        self.matches = {}  # ID number => Match object

        self.state = BracketState.Suspended

        # UpdateParams members' values are their index in the list sent by sendUpdate.  Find the largest one to calculate
        # the length of the update list.
        self.updateListLength = 1 + max(val for nm, val in UpdateParams.__dict__.items() if nm[0] != "_" and isinstance(val, int))

        self.cancelReports = False  # flag to cancel sending score reports to challonge

        self.mainThread = threading.Thread(target=self._execute)
        self.mainThread.daemon = True
        self.mainThread.start()

        self.challongeReportThread = threading.Thread(target=self._report)
        self.challongeReportThread.daemon = True
        self.challongeReportThread.start()

    def receiveMessage(self, client, prefix, params):

        if prefix == Message.RequestUpdate:
            self.sendUpdate(name=client.nick)

        elif prefix == Message.ToggleRegistration:
            """ Player has clicked 'Register'/'Unregister' button.  Change their status here and at challonge."""

            if self.state == BracketState.Registration:
#                if client.nick in self.kickedPlayers:
#                    self.manager.chatMessage(client, "You have been kicked from this tournament.")
#                    self.sendUpdate(name=client.nick)

                if client.nick in self.pendingRegistrations:
                    # Registration button disables itself while waiting for a reply, so this shouldn't happen normally.
                    self.manager.chatMessage(client, "Request already pending.  Please wait.")
                    self.sendUpdate(name=client.nick)

                else:
                    self.pendingRegistrations.add(client.nick)

                    # Registrations done on a separate thread to prevent a backlog from forming as players wait for the
                    # challonge calls for other players to go through.  Threads should only live for a few seconds and
                    # therefore very few should be alive at any one time.
                    try:
                        thd = threading.Thread(target=self.toggleRegistration, args=(client,))
                        thd.daemon = True
                        thd.start()
                    except:
                        self.manager.chatMessage(client, "Error processing request, please retry.")

            elif self.state == BracketState.Open or self.state == BracketState.Suspended:
                self.manager.chatMessage(client, "Too late!  Tournament already active.")
                self.sendUpdate(name=client.nick)

        elif prefix == Message.ToggleReady:
            """ Player has clicked the 'Ready' button for their current match.  Relay message to the appropriate Match object. """
            if self.state == BracketState.Open:
                if self.toggleReady(client.nick):
                    return

            self.sendUpdate(name=client.nick)

        elif prefix == Message.ScoreReport:
            """ Player has reported a score for their current match.  Verify the scores make sense and send them to the appropriate Match object. """
            if self.state == BracketState.Open:
                if client.status == 2:
                    self.manager.chatMessage(client, "Can't submit report while playing.")
                    self.sendUpdate(name=client.nick)
                    return

                me, opp = params
                match, p = self.nextMatch(client.nick)

                if match == None or match.state != MatchState.Open:
                    self.manager.chatMessage(client, "Error: Not currently in a match.")
                else:
                    score = {p: me, (3 - p): opp}

                    if not self.isValidScore(score):
                        self.manager.chatMessage(client, "Invalid scores")
                    else:
                        match.setReportedResult(p, score)

                self.sendUpdate(name=client.nick)


        elif prefix == Message.ModeratorRequestInfo:
            """ Moderator has requested info on current open matches and active players. """
            if client.nick not in self.moderators:
                self.manager.sendMessage(client, Message.ModeratorInfoResponse, None)
            else:
                self.manager.sendMessage(client, Message.ModeratorInfoResponse, self._gatherModeratorInfo())

        elif prefix == Message.ModeratorScoreReport:
            """ Moderator has submitted a score for an open match.  Check it and send it to the Match object."""
            if self.state == BracketState.Open:
                if client.nick not in self.moderators:
                    self.manager.chatMessage(client, "You are not a moderator for this tournament.")
                    return

                try:
                    names = {1: params[0][0], 2: params[0][1]}
                    score = {1: params[1][0], 2: params[1][1]}
                    match, _ = self.nextMatch(names[1])

                    if match == None or \
                       match.state != MatchState.Open or \
                       match.pName != names or \
                       not self.isValidScore(score):
                        raise
                except:
                    self.manager.sendMessage(client, Message.ModeratorScoreReportResponse, False)
                    self.manager.sendMessage(client, Message.ModeratorInfoResponse, self._gatherModeratorInfo())
                    return

                match.setResult(score, "Match result has been set by moderator.")
                self.manager.sendMessage(client, Message.ModeratorScoreReportResponse, True)

        # elif prefix == Message.ModeratorKickPlayer:
        #     """ Moderator has kicked a player. """
        #     name = params
        #
        #     self.kickedPlayers.add(name)
        #
        #     client = self.manager.clientFromName(name)
        #     self.manager.chatMessage(client, "You have been kicked from the tournament.")
        #
        #     if self.state == BracketState.Registration and name in self.participants:
        #         self.toggleRegistration(client)
        #     elif self.state == BracketState.Open and name in self.participants:
        #         match, pos = self.nextMatch(name)
        #         if match != None:
        #             match.setResult({pos:0, (3-pos):1}, Kicked=True)
        #
        #     self.manager.sendMessage(client, Message.ModeratorKickPlayerResponse, True)
        #

    def _execute(self):
        """ Manages the execution of the tournament.
            This method is called in a new thread at tournament creation and doesn't return until the tournament is over.
        """

        """ Phase 1 - Registration (BracketState.Registration)
                In this phase registration requests will be received and processed.
                Tasks:
                    - Create a Challonge bracket.
                    - Notify all clients in the room that a tournament is starting. (Signal client to display registration UI)
                    - Set state variable to allow processing of registration requests.
                    - After 30 minutes (REGISTRATION_TIME), signal clients that registration period is over (hide registration UI)
        """

        logging.info("Starting tournament " + self.name)

        """Create a tournament on challonge"""
        for attempts_left in range(4, -1, -1):
            try:
                # the API won't create a random URL for us, so do it ourselves.
                self.urltail = "".join((random.choice(string.letters + string.digits) for _ in range(8)))
                self.challongeData = challonge.tournaments.create(name=self.name[:60], url=self.urltail,
                                                                  tournament_type="double elimination")
                logging.info("Bracket created successfully at %s" % (self.challongeData["full-challonge-url"],))
                break

            except Exception as e:
                # noinspection PyUnresolvedReferences
                if isinstance(e, HTTPError) and e.code == 401:
                    logging.info(
                        "Unexpected error creating bracket: Challonge credentials invalid.  Canceling tournament %s." % self.name)
                    self.cancel()
                    return

                if not (isinstance(e, ChallongeException) and e.message == "URL has already been taken"):
                    try:  # in case it actually got created anyway, try to delete it so it's not cluttering up our tournament list
                        challonge.tournaments.destroy(self.urltail)
                    except:
                        pass

                if attempts_left > 0:
                    logging.info("Exception '%s, %s' creating bracket for %s.  Retrying in 5 seconds (%s attempts left)." %
                                            (repr(e), e.message, self.name, attempts_left,))
                    time.sleep(5)
                else:
                    logging.info("Exception '%s, %s' creating bracket for %s.  Cancelling Tournament" %
                                            (repr(e), e.message, self.name,))
                    self.cancel()
                    return

        self.state = BracketState.Registration  # open for business!

        for client in self.manager.ggpochannel.clients:  # tell everyone
            self.onChannelJoin(client)

        time.sleep(REGISTRATION_TIME)

        if self.state == BracketState.Completed:  # canceled during the sleep?
            return

        """ Phase 2 - Tournament (BracketState.Open)
                In this phase match status updates are sent to participants and score reports are processed.
                Tasks:
                    - Signal Challonge that registration is finished and matches will start.
                    - Get final bracket and participant data from Challonge and import it into our own structures
                    - Send initial match notifications to all participants.
        """

        self.state = BracketState.Suspended  # pause everything until we're ready

        print self.participants
        if len(self.participants) < MINIMUM_PARTICIPANTS:
            self.manager.broadcastChatMessage("Tournament cancelled, insufficient participants")
            logging.info("Tournament %s in room %s cancelled due to insufficient participants" % (self.name, self.channel))
            self.cancel()
            return

        for attempts_left in range(4, -1, -1):
            try:
                challonge.tournaments.start(self.urltail)  # Calling this redundantly won't cause an error

                """ Challonge has now started the tournament and we can retrieve the final bracket and participant
                    list.  Check the retrieved data for consistency and completeness, generate an array of Match objects
                    from the bracket data, and start the first matches.
                """
                rawParticipants = challonge.participants.index(self.urltail)
                rawMatches = challonge.matches.index(self.urltail)

                # check list of participants matches
                participants1 = {p["id"] for p in rawParticipants}
                participants2 = ({m["player1-id"] for m in rawMatches} | {m["player2-id"] for m in rawMatches}) - {None}
                if participants1 != participants2:
                    raise Exception("Inconsistent data: Participant list doesn't match bracket")

                participants = {p["id"]: p["name"] for p in rawParticipants}
                matches = {m["id"]: Match(self, m) for m in rawMatches}
                finalRound = max({m.round for m in matches.values()})

                # find and mark the two grand finals matches
                gfMatches = filter(lambda match:match.round == finalRound, matches.values())
                if len(gfMatches) != 2:
                    raise Exception("Wrong number of grand finals matches.")

                gfMatches[0].isGFinals = gfMatches[1].isGFinals = True

                if gfMatches[0].prereqMatchID == {i:gfMatches[1].mID for i in (1, 2)}:
                    gfMatches[0].gfRound, gfMatches[1].gfRound = 2, 1
                elif gfMatches[1].prereqMatchID == {i:gfMatches[0].mID for i in (1, 2)}:
                    gfMatches[0].gfRound, gfMatches[1].gfRound = 1, 2
                else:
                    raise Exception("Grand finals matches are screwed up.")

                # As received from challonge, each match record has the IDs of its ancestor matches, but not its
                # descendant matches.  Go through and fill in the missing info.
                for m in matches.values():
                    m.forwardLink(matches, finalRound)

                # check that the bracket is complete
                for m in matches.values():
                    if m.pName[1] == None and m.prereqMatch[1] == None or \
                       m.pName[2] == None and m.prereqMatch[2] == None:
                        raise Exception("Inconsistent data: Ancestor match missing.")

                    if not m.isGFinals:
                        if m.wNextMatch == None or (m.round > 0 and m.lNextMatch == None):
                            raise Exception("Inconsistent data: Descendant match missing")
                    else:
                        if m.gfRound == 1 and (m.wNextMatch == None or m.lNextMatch == None):
                            raise Exception("Inconsistent data: Descendant match missing")

                # Everything checks out.  Save bracket and activate first matches.
                with synchronized(self.manager):
                    self.participants = participants
                    self.finalRound = finalRound
                    self.matches = matches

                    for m in self.matches.values():
                        m.finalizeInitialization()

                break

            except Exception as e:
                # noinspection PyUnresolvedReferences
                if isinstance(e, HTTPError) and e.code == 401:
                    logging.error("Fatal error, Challonge credentials invalid.  Canceling tournament %s." % self.name)
                    self.cancel()
                    return

                if attempts_left > 0:
                    logging.info("Exception '%s, %s' starting tournament %s.  Retrying in 10 seconds (%s attempts left)." % \
                                            (repr(e), e.message, self.name, attempts_left,))
                    time.sleep(10)
                else:
                    logging.error("Exception '%s, %s' starting tournament %s.  No retries left." % \
                                            (repr(e), e.message, self.name,))
                    self.cancel()
                    return

            finally:
                if self.state == BracketState.Completed:
                    self.manager.broadcastChatMessage("Server error starting tournament.  Tournament canceled. :(",
                                                      names=self.participants.values())

        logging.info("Tournament %s started successfully" % (self.name,))

        self.state = BracketState.Open

#        for client in ifilter(lambda cl: cl.nick in self.participants.values(), self.manager.ggpochannel.clients):
        for client in self.manager.ggpochannel.clients:
            self.onChannelJoin(client)

        """ From here, everything is done through timers or client requests, but if somehow everything hasn't
            finalized after a long time, try to abort and clean up.
        """

        time.sleep(6 * 60 * 60)

        if self.state != BracketState.Completed:
            self.cancel()

    def _report(self):
        # predicate to select matches whose scores can be submitted to challonge
        isReportable = lambda m: (m.canSubmit and not m.hasSubmitted) and \
                                 (m.prereqMatch[1] == None or m.prereqMatch[1].hasSubmitted) and \
                                 (m.prereqMatch[2] == None or m.prereqMatch[2].hasSubmitted)

        while not self.cancelReports:
            match = next(ifilter(isReportable, self.matches.values()), None)

            if match == None:
                if self.state == BracketState.Completed:
                    # No reportable matches but tournament is over, therefore we're done. Finalize results and leave.
                    try:
                        # they forgot to include a nice api call for this :(
                        challonge.api.fetch("POST", "tournaments/%s/finalize" % self.urltail)
                        return
                    except:
                        pass

                time.sleep(5)

            else:
                try:
                    challonge.matches.update(self.urltail, match.mID, scores_csv=match.scoresCsv, winner_id=match.winnerID)
                    match.hasSubmitted = True
                except HTTPError:
                    time.sleep(10)
                except Exception as e:
                    logging.error("Unexpected error in Bracket._report %s" % (repr(e)))
                    time.sleep(10)

    def reportWinner(self, winner):
        self.winner = winner

        self.manager.broadcastMessage(Message.TournamentOver, winner)
        self.manager.reportEnding(self)
        self.state = BracketState.Completed

        # If challonge reports are lagging behind for whatever reason, wait a few minutes before giving up.
        def f():
            self.cancelReports = True

        thd = self.manager.Timer(300, f)
        thd.daemon = True
        thd.start()

    def cancel(self):
        self.state = BracketState.Completed
        self.manager.reportEnding(self)
        self.manager.broadcastMessage(Message.TournamentOver, None)
        self.cancelReports = True
        for match in self.matches:
            match.cancel()

    def toggleRegistration(self, client):
        """ Register the user if they're not registered, or unregister them if they are. """
        name = client.nick

        # try to find user in list of registered participants
        pID = next(ifilter(lambda (p, n): n == name, self.participants.items()), (None, None))[0]

        try:
            if pID != None:  # user is already registered, so unregister them.
                try:
                    challonge.participants.destroy(self.urltail, pID)
                except HTTPError as e:
                    if e.code != 404:  # 404 errors are challonge's way of telling us that user is already not registered
                        raise

                self.manager.chatMessage(client, "You have been unregistered.")
                self.participants.pop(pID, None)

            else:  # user isn't registered, so register them.
                # if client.nick in self.kickedPlayers:
                #     self.manager.chatMessage(client, "Cannot register.  You have been kicked from this tournament.")
                #     return

                try:
                    self.participants[challonge.participants.create(self.urltail, name)["id"]] = name
                except ChallongeException as e:
                    if e.message == "Name has already been taken":
                        # Name was already registered but we don't have the ID.  Retrieve full participant list to find it
                        t = next(ifilter(lambda p: p["name"] == name, challonge.participants.index(self.urltail)), None)
                        self.participants[t["id"]] = name
                    else:
                        raise

                self.manager.chatMessage(client, "You have been registered.")

        except ChallongeException as e:
            if e.message == "Tournament participants can no longer be added":
                self.manager.chatMessage(client, "Too late, tournament has already started!")

        except Exception:
            self.manager.chatMessage(client, "Error processing request, please retry.")

        self.sendUpdate(name)
        self.pendingRegistrations.discard(name)

    def toggleReady(self, name, state=None):
        """ Toggle the ready status of name, or set it to a value if state is True or False """
        m, p = self.nextMatch(name)
        return m != None and m.toggleReady(p, state)

    def sendUpdate(self, name=None, match=None):
        """ Send data to a participant with their current status.

            Specify at least one of name and match.  If match is specified but not name, both participants in the match
            are sent updates.
        """

        if match == None and name == None:
            return
        elif name == None:
            if match.pName[1] != None: self.sendUpdate(name=match.pName[1])
            if match.pName[2] != None: self.sendUpdate(name=match.pName[2])
            return

        client = self.manager.clientFromName(name)
        if client == None:
            return

        outparams = [0] * self.updateListLength

        # if client.nick in self.kickedPlayers:
        #     outparams[UpdateParams.Status] = UpdateStatus.NoMatch

        if self.state == BracketState.Suspended:
            outparams[UpdateParams.Status] = UpdateStatus.NoMatch

        elif self.state == BracketState.Registration:
            outparams[UpdateParams.Status] = UpdateStatus.RegistrationRegistered if name in self.participants.values() else \
                                             UpdateStatus.RegistrationNotRegistered
            outparams[UpdateParams.RegistrationTimeout] = int((self.startTime - datetime.datetime.now(tzlocal())).total_seconds())

        elif self.state == BracketState.Open:
            match, pos = self.nextMatch(name)
            print name, match

            if match == None:
                outparams[UpdateParams.Status] = UpdateStatus.NoMatch
            else:
                match.buildUpdate(pos, outparams)

        else:
            return

        self.manager.sendMessage(client, Message.Update, outparams)

    def onChannelJoin(self, client):
        self.manager.sendMessage(client, Message.TournamentInfo, [self.name, self.urltail, self.moderators, self.rules])
        self.sendUpdate(name=client.nick)
        if client.nick in self.moderators:
            self.manager.chatMessage(client, MODERATOR_WELCOME_MESSAGE)

    def onStatusChanged(self, client):
        if client.status != 0 and self.state == BracketState.Open and client.nick in self.participants.values():
            self.toggleReady(client.nick, state=False)

    def onChannelLeaving(self, client):
        if self.state == BracketState.Open and client.nick in self.participants.values():
            self.toggleReady(client.nick, state=False)

    # noinspection PyUnresolvedReferences
    def onMatchStarting(self, quark):
        match, _ = self.nextMatch(quark.p1client.nick)
        if match != None and set(match.pName.values()) == {quark.p1client.nick, quark.p2client.nick}:
            match.hasStarted()

    def _gatherModeratorInfo(self):
#        if self.state == BracketState.Registration:
#            dataParticipants = self.participants.values()
#        else:
#            dataParticipants = filter(lambda nm:self.nextMatch(nm) != (None, None), self.participants.values())

        dataMatches = [[m.pName[1], m.pName[2],
                        m.reportedResult[1][1], m.reportedResult[1][2],
                        m.reportedResult[2][1], m.reportedResult[2][2],
                        m.refResult[1], m.refResult[2]]
                        for m in self.matches.values() if m.state == MatchState.Open]

#        return [dataParticipants, dataMatches]
        return dataMatches

    @staticmethod
    def isValidScore(score):
        """ Check that score contains a valid match score """
        # noinspection PyChainedComparisons
        return isinstance(score, dict) and \
               set(score.keys()) == {1,2} and \
               isinstance(score[1], int) and \
               isinstance(score[2], int) and \
               score[1] >= 0 and \
               score[2] >= 0 and \
               score[1] != score[2]

    # edited directly by Match objects
    nextMatchDict = {}  # name => (match, side) with match being the current open match for that user

    def nextMatch(self, name):
        return self.nextMatchDict.get(name, (None, 0))


# noinspection PyClassHasNoInit
class MatchState:
    Pending, Open, Completed = range(3)
    StringDict =  {"pending": Pending, "open": Open, "completed": Completed}


class Match(object):
    """ Handles data for an individual match within the tournament.
            - keeps track of ready status and starts match when both participants are ready
            - cancels match if players aren't ready in time or don't report a score in time
            - receives score reports
            - notifies successsor matches when completed
    """

    def __init__(self, bracket, m):
        self.rawMatch = m  # unaltered match dict received from challonge

        self.bracket = bracket
        self.manager = bracket.manager
        self.channel = bracket.channel
        self.ggpochannel = self.manager.ggpochannel

        # deliberately let potential KeyErrors happen so bracket will know if the data is incomplete
        self.state = MatchState.StringDict[m["state"]]
        self.mID = m["id"]
        self.round = m["round"]
        self.prereqMatchID = {1: m["player1-prereq-match-id"], 2: m["player2-prereq-match-id"]}
        self.prereqMatch = {1: None, 2: None}
        self.playerIsPrereqMatchLoser = {1: m["player1-is-prereq-match-loser"], 2: m["player2-is-prereq-match-loser"]}
        self.pID = {1: m["player1-id"], 2: m["player2-id"]}
        self.pName = {1: None if self.pID[1] == None else self.bracket.participants[self.pID[1]],
                      2: None if self.pID[2] == None else self.bracket.participants[self.pID[2]],}
        self.wNextMatch = None
        self.lNextMatch = None

        self.ready = {1: False, 2: False}
        self.refResult = {1: 0, 2: 0}
        self.reportedResult = {1: EMPTY_RESULT, 2: EMPTY_RESULT}
        self.result = EMPTY_RESULT
        self.scoresCsv = None
        self.winnerID, self.winnerName = None, None
        self.loserID, self.loserName = None, None
        self.startDeadline = None
        self.finishDeadline = None
        self.started = False
        self.hasSubmitted = False
        self.canSubmit = False
        self.roundID = -1
        self.gfRound = -1
        self.isGFinals = False
        self.canceled = False

        self.startTimeoutTimer = None
        self.finishTimeoutTimer = None

    def cancel(self):
        # Bracket notifying us that tournament has been canceled.
        # Needed for the start/finish timer callbacks, since they're the only points of entry not through the bracket
        self.canceled=True

    def forwardLink(self, matchDict, finalRound):
        """ Get match objects for preceeding matches and tell following matches who we are. """

        self.prereqMatch = {1: None if self.prereqMatchID[1]==None else matchDict[self.prereqMatchID[1]],
                            2: None if self.prereqMatchID[2]==None else matchDict[self.prereqMatchID[2]]}

        for pos, m in self.prereqMatch.items():
            if m != None:
                if self.playerIsPrereqMatchLoser[pos]:
                    m.lNextMatch = self
                else:
                    m.wNextMatch = self

        # figure out which round we're in based on the confusing challonge numbering system
        t = finalRound - abs(self.round)
        if self.round < 0:
            self.roundID = {0: Rounds.LosersFinals, 1: Rounds.LosersSemifinals}.get(t, Rounds.LosersBracket)
        elif self.isGFinals:
            self.roundID = {1: Rounds.GrandFinals1, 2:Rounds.GrandFinals2}[self.gfRound]
        else:
            self.roundID = {1: Rounds.WinnersFinals, 2: Rounds.WinnersSemifinals}.get(t, Rounds.WinnersBracket)

    def finalizeInitialization(self):
        # Bracket has initialized successfully.  Begin execution
        if self.pName[1] != None: self.bracket.nextMatchDict[self.pName[1]] = (self, 1)
        if self.pName[2] != None: self.bracket.nextMatchDict[self.pName[2]] = (self, 2)

        if self.state == MatchState.Open:
            # if self.pName[1] in self.bracket.kickedPlayers:
            #     self.setResult({1:0, 2:1}, Kicked=True)
            # elif self.pName[2] in self.bracket.kickedPlayers:
            #     self.setResult({1:1, 2:0}, Kicked=True)
            # else:
            self.initializeStartTimer()

        # first updates will be sent by bracket

    def initializeStartTimer(self):
        # timer to cancel match if players don't ready up in time.
        if self.startTimeoutTimer == None:
            self.startDeadline = datetime.datetime.now() + datetime.timedelta(seconds=READY_TIMEOUT)
            self.startTimeoutTimer = self.manager.Timer(READY_TIMEOUT, self.startTimeout)
            self.startTimeoutTimer.daemon = True
            self.startTimeoutTimer.start()

    def startTimeout(self):
        # players didn't ready up in time.  Give match to whoever is ready now, or decide randomly if neither is.
        with synchronized(self.manager):
            if self.started or self.state != MatchState.Open or self.canceled:
                return

            if set(self.ready.values()) == {True, False}:  # One ready, one not.
                p = 1 if self.ready[1] else 2
                self.setResult({p: 1, (3 - p): 0}, "%s did not go to Ready in time." % self.pName[3-p])

            else:  # Both must be unready, otherwise toggleReady would've set self.started
                p = random.randint(1, 2)
                self.setResult({p: 1, (3 - p): 0}, "Neither player was set to Ready by deadline.  Winner has been randomly chosen.")

    def initializeFinishTimer(self):
        # timer to cancel match if not completed in time
        if self.finishTimeoutTimer == None:
            tmp = MATCH_FINISH_TIMEOUT if not self.isGFinals else 2*MATCH_FINISH_TIMEOUT
            self.finishDeadline = datetime.datetime.now() + datetime.timedelta(seconds=tmp)
            self.finishTimeoutTimer = self.manager.Timer(MATCH_FINISH_TIMEOUT, self.finishTimeout)
            self.finishTimeoutTimer.daemon = True
            self.finishTimeoutTimer.start()

    def finishTimeout(self):
        # match deadline passed without agreeing score reports being submitted.  Figure out who to set as winner.
        with synchronized(self.manager):
            if self.state != MatchState.Open or self.canceled:
                return

            clients = [self.manager.clientFromName(self.pName[1]),
                       self.manager.clientFromName(self.pName[2])]

            if self.reportedResult.values().count(EMPTY_RESULT) == 1:
                # if only one player submitted a score, accept that score.
                res = self.reportedResult[1 if self.reportedResult[2] == EMPTY_RESULT else 2]
                msg = "Only one score report was received by deadline."

            elif self.bracket.isValidScore(self.refResult):
                # if a monitored score is available, use that
                res = self.refResult
                msg = "Agreeing score reports were not received by deadline.  The winner has been decided automatically."

            elif len(set(clients) & self.ggpochannel.clients) == 1:
                # if both or neither submitted scores but one player left, let the one who stayed win.
                p = 1 if clients[0] in self.ggpochannel.clients else 2
                res = self.reportedResult[p] if self.reportedResult[p] != EMPTY_RESULT else {p:1, (3-p):0}
                msg = "Agreeing score reports were not received by deadline.  The winner has been randomly chosen."

            else:
                # just pick someone
                p = random.randint(1, 2)
                res = self.reportedResult[p] if self.reportedResult[p] != EMPTY_RESULT else {p:1, (3-p):0}
                msg = "Agreeing score reports were not received by deadline.  The winner has been randomly chosen."

            self.setResult(res, msg)

    def setReportedResult(self, pos, res):
        # score report has been submitted, check if it agrees with opponent's report and set the result if it does

        self.reportedResult[pos] = res  # result should be validated before we are called

        if self.reportedResult[1] == self.reportedResult[2]:
            #results are the same, set result and finalize match
            self.setResult(self.reportedResult[1], "Score report accepted.")

        elif EMPTY_RESULT in self.reportedResult.values():
            # first report received
            self.bracket.sendUpdate(match=self)

        elif (self.reportedResult[1][1] < self.reportedResult[1][2]) == (self.reportedResult[2][1] < self.reportedResult[2][2]):
            # reports don't match but they agree on who won.  Pick one at random.
            self.setResult(self.reportedResult[random.randint(1,2)], "Reports did not match but agreed on the winner. Accepted score randomly chosen.")

        else:
            # Conflicting reports.
            self.manager.broadcastChatMessage(CONFLICTING_SCORES_MSG.format(names=self.pName, scores=self.reportedResult),
                                                    names=self.pName.values())
            self.manager.broadcastChatMessage(CONFLICTING_SCORES_MOD_MSG.format(names=self.pName), names=self.bracket.moderators)
            self.bracket.sendUpdate(match=self)

    def setResult(self, res, msg=None):
        """ Set the result for the match.  Tell succeeding matches we are finished.  Signal challonge reporting loop
            (Bracket._report) that our score is ready to be uploaded.
        """

        if self.startTimeoutTimer != None:
            self.startTimeoutTimer.cancel()
        if self.finishTimeoutTimer != None:
            self.finishTimeoutTimer.cancel()

        wpos, lpos = (1, 2) if res[1] > res[2] else (2, 1)

        self.state = MatchState.Completed
        self.result = res
        self.scoresCsv = "%s-%s" % (res[1], res[2])
        self.winnerID, self.winnerName = self.pID[wpos], self.pName[wpos]
        self.loserID, self.loserName = self.pID[lpos], self.pName[lpos]
        self.bracket.nextMatchDict.pop(self.pName[1], None)
        self.bracket.nextMatchDict.pop(self.pName[2], None)

        if msg != None:
            self.manager.broadcastChatMessage(msg, names=self.pName.values())

        self.manager.broadcastChatMessage("A score of {n[1]} - {s[1]}, {n[2]} - {s[2]} has been recorded.".format(n=self.pName, s=self.result),
                                            names=self.pName.values())

        if self.isGFinals and ((self.gfRound == 1 and wpos == 1) or self.gfRound == 2):
            self.bracket.reportWinner(self.winnerName)
        else:
            if self.lNextMatch != None:
                self.lNextMatch.prereqMatchFinished(self)
            if self.wNextMatch != None:
                self.wNextMatch.prereqMatchFinished(self)

            self.bracket.sendUpdate(match=self)

        self.canSubmit = True  # mark match as finalized and ready to be reported

    def toggleReady(self, pos, state):
        # ready/unready button was clicked.  Start match if both players are ready.
        if self.state != MatchState.Open:
            return False

        self.ready[pos] = state if state != None else not self.ready[pos]

        if self.ready[1] and self.ready[2]:
            clients = [self.manager.clientFromName(self.pName[1]),
                       self.manager.clientFromName(self.pName[2])]

            try:
                self.manager.createMatch(clients, self.matchCallback, randomizeSides=True)

                if self.startTimeoutTimer != None:
                    self.startTimeoutTimer.cancel()
                self.started = True
                self.initializeFinishTimer()

            except MatchError as e:
                if e.code == MatchErrorCode.P1Unavailable or e.code == MatchErrorCode.BothPlayersUnavailable:
                    self.ready[1] = False
                if e.code == MatchErrorCode.P2Unavailable or e.code == MatchErrorCode.BothPlayersUnavailable:
                    self.ready[2] = False

        self.bracket.sendUpdate(match=self)

        return True

    def hasStarted(self):
        """ Players have started by challenging each other instead of clicking ready. """
        if self.startTimeoutTimer != None:
            self.startTimeoutTimer.cancel()
        self.started = True
        self.initializeFinishTimer()
        self.bracket.sendUpdate(match=self)

    # noinspection PyUnusedLocal
    def matchCallback(self, match, evt, evtparams):
        if evt == MatchEvent.GameEnded:
            self.refResult = match.gameScore

    def buildUpdate(self, pos, outparams):
        # populate outparams with data about the match.
        outparams[UpdateParams.SelfReady] = self.ready[pos]
        outparams[UpdateParams.OppReady] = self.ready[3 - pos]
        outparams[UpdateParams.PrereqLoser] = self.playerIsPrereqMatchLoser[3 - pos]
        outparams[UpdateParams.RoundID] = self.roundID

        if self.state == MatchState.Open:
            outparams[
                UpdateParams.Status] = UpdateStatus.MatchOpenStarted if self.started else UpdateStatus.MatchOpenNotStarted
            outparams[UpdateParams.OppName] = self.pName[3 - pos]
            outparams[UpdateParams.MeReport] = [self.reportedResult[pos][pos],
                                                self.reportedResult[pos][3 - pos]]
            outparams[UpdateParams.OppReport] = [self.reportedResult[3 - pos][pos],
                                                 self.reportedResult[3 - pos][3 - pos]]
            outparams[UpdateParams.StartTimeout] = 0 if self.started else \
                                                    int((self.startDeadline - datetime.datetime.now()).total_seconds())
            outparams[UpdateParams.FinishTimeout] = 0 if not self.started else \
                                                    int((self.finishDeadline - datetime.datetime.now()).total_seconds())

        elif self.state == MatchState.Pending:
            if self.prereqMatch[3 - pos].state == MatchState.Pending:
                outparams[UpdateParams.Status] = UpdateStatus.MatchPendingPrereqPending
            else:
                outparams[UpdateParams.Status] = UpdateStatus.MatchPendingPrereqOpen
                outparams[UpdateParams.PrereqP1Name] = self.prereqMatch[3 - pos].pName[1]
                outparams[UpdateParams.PrereqP2Name] = self.prereqMatch[3 - pos].pName[2]

    def prereqMatchFinished(self, match):
        # one of our prerequisite matches has ended.  Update participant info and check if we can start.
        for i in (1, 2):
            if match == self.prereqMatch[i]:
                if self.playerIsPrereqMatchLoser[i]:
                    self.pID[i], self.pName[i] = match.loserID, match.loserName
                else:
                    self.pID[i], self.pName[i] = match.winnerID, match.winnerName

                self.bracket.nextMatchDict[self.pName[i]] = (self, i)

        if None not in self.pID.values():
            self.state = MatchState.Open

            # if self.pName[1] in self.bracket.kickedPlayers:
            #     self.setResult({1:0, 2:1}, Kicked=True)
            #     return
            # elif self.pName[2] in self.bracket.kickedPlayers:
            #     self.setResult({1:1, 2:0}, Kicked=True)
            #     return

            # skip readying up for second set of grand finals
            if not (self.isGFinals and self.gfRound == 2):
                self.initializeStartTimer()
            else:
                self.started = True
                self.initializeFinishTimer()

            # update people waiting on the results of this match.
            self.bracket.sendUpdate(match=self.wNextMatch)
            self.bracket.sendUpdate(match=self.lNextMatch)

        self.bracket.sendUpdate(match=self)


MODERATOR_WELCOME_MESSAGE = \
    "<font color=red>MODERATOR INSTRUCTIONS:</font> You are listed as a moderator for current tournament. " +\
    "By clicking the 'Moderator Tools' button at the top of the screen you can submit a score for any open " +\
    "match.  You will receive notices if conflicting score reports have been submitted for a match.<br><br>" +\
    "Thank you for your help."

CONFLICTING_SCORES_MSG =\
    "<font color=red>Conflicting score reports received.</font><br>" +\
    "{names[1]} reports {names[1]} - {scores[1][1]}, {names[2]} - {scores[1][2]}<br>" +\
    "{names[2]} reports {names[1]} - {scores[2][1]}, {names[2]} - {scores[2][2]}<br>" +\
    "If the conflict is not resolved by the competitors or a moderator before the match reporting deadline, " +\
    "the winner will be chosen randomly.<br><br>" +\
    "Click 'Rules' to see the moderators for this tournament."

CONFLICTING_SCORES_MOD_MSG = \
    "<font color=red>Automated Moderator Notice: Conflicting scores have been reported " +\
    "in the match between {names[1]} and {names[2]}.</font></br>" +\
    "Someone might have just made a typo or there might be a real conflict. Please try to resolve the situation or " +\
    "set the score yourself in the Moderator Tools window. If you cannot determine who won, just do nothing and " +\
    "the winner will be decided by the server when the match reporting deadline passes.  Thank you."

# noinspection PyClassHasNoInit
class Message:
    # server-to-client messages
    TournamentInfo = 1
    Update = 2
    TournamentOver = 3
    ModeratorInfoResponse = 4
    ModeratorScoreReportResponse = 5
#    ModeratorKickPlayerResponse = 6

    # client-to-server messages
    ToggleRegistration = 101
    ToggleReady = 102
    ScoreReport = 103
    ModeratorRequestInfo = 104
    ModeratorScoreReport = 105
#    ModeratorKickPlayer = 106
    RequestUpdate = 107


# noinspection PyClassHasNoInit
class UpdateParams:
    Status = 0                  # One of UpdateStatus
    OppName = 1                 # Name of opponent
    SelfReady = 2               # If user is set as ready or not
    OppReady = 3                # If user's opponent is set as ready
    RegistrationTimeout = 4     # Seconds until registration period ends
    StartTimeout = 5            # Seconds until players must ready up and start match
    FinishTimeout = 6           # Seconds until players must finish playing match
    PrereqP1Name = 7            # Names of players in match which will determine opponent
    PrereqP2Name = 8            #
    PrereqLoser = 9             # Whether opponent will be the loser of the prereq match
    RoundID = 10                # One of Rounds
    MeReport = 11               # Score reported by user
    OppReport = 12              # Score reported by opponent


# noinspection PyClassHasNoInit
class Rounds:
    LosersFinals = 1
    LosersSemifinals = 2
    LosersBracket = 3
    WinnersFinals = 4
    WinnersSemifinals = 5
    WinnersBracket = 6
    GrandFinals1 = 7
    GrandFinals2 = 8


# noinspection PyClassHasNoInit
class UpdateStatus:
    RegistrationNotRegistered = 1   # Registration period, you are not registered
    RegistrationRegistered = 2      # Registration period, you are registered
    MatchOpenNotStarted = 3         # Match available, not started yet
    MatchOpenStarted = 4            # Match in progress
    MatchPendingPrereqPending = 5   # Next opponent is winner/loser of a match in progress
    MatchPendingPrereqOpen = 6      # Next opponent is TBD
    NoMatch = 7                     # No matches for this user
