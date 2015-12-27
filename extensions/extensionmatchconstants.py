""" Constants used by ExtensionMatch objects

    No need to import this directly, it will be imported with "from extensionbase import *"
"""

class MatchError(Exception):
    def __init__(self, code):
        Exception.__init__(self)
        self.code = code

# noinspection PyClassHasNoInit
class MatchErrorCode:
    """ Used for the 'code' member of MatchError or as additional info for some match events (below) """
    Timeout = 0
    P1Unavailable = 1
    P2Unavailable = 2
    BothPlayersUnavailable = 3
    P1ClosedEmulator = 4
    P2ClosedEmulator = 5

# noinspection PyClassHasNoInit
class MatchEvent:
    """  Event codes emitted by matches.  Each code will come with a tuple containing additional
         information, if applicable.  If a code sends additional data, it is described after the definition of the code.
         If a code has no additional data, an empty tuple is sent.

         The sequence these codes are sent in is described below.
    """

    """ Step 1) Startup result

    """

    # Emulation has started successfully and the game is actually running.
    EmulationStarted = 100
    # Match failed to start but will be reattempted.
    StartupFailedRetrying = 102  # args = (member of MatchStartError,)
    # Match failed to start and will not be reattempted.  This will end the match.
    StartupFailedNotRetrying = 103  # args = (member of MatchStartError,)

    """ Step 2) Calibration

        This step is only reached if EmulationStarted was sent in the last step
    """
    # We will be able to send in-game events for this match
    CalibrationSuccess = 200
    # We will *not* be able to send in-game events for this match
    CalibrationFail = 201

    """ Step 3) Game Events

        If CalibrationSuccess was sent in the last step, these codes will be sent repeatedly until the emulator is closed.

        If CalibrationFail was sent in the last step, no events at all will be sent for this step and no more events
        will be sent until step 5 below.

        GameStarted will be sent before the first RoundStarted, and the last RoundEnded will be sent before GameEnded
    """

    # A game (i.e. one best-of-3-rounds fight) has started
    GameStarted = 300
    # A round has started
    RoundStarted = 301
    # Player health has changed
    HealthChanged = 302       # args = (health player 1, health player 2)  Health is given on scale of 0-100
    # Timer has changed
    TimerChanged = 303        # args = (timer,)
    # A round has ended
    RoundEnded = 304          # args = (winning side,)
    # A game has ended
    GameEnded = 305           # args = (winning side,)
    # Reset button has been pressed
    ResetPressed = 306
    # A player has closed the emulator.  This will end the match.
    EmulatorClosed = 307      # args = (side of player who closed, or 0 if it was closed
