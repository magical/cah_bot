import time
import random
import re
import urllib2
from collections import defaultdict

from hamper.interfaces import ChatCommandPlugin, Command
from hamper.utils import ude
from sqlalchemy import Column, Integer, String, Boolean, desc
from sqlalchemy.ext.declarative import declarative_base
from twisted.internet import reactor


SQLAlchemyBase = declarative_base()

# BLANK is the magic marker used to denote blanks
# in the card descriptions.
BLANK = "_" * 10

class CardsAgainstHumanity(ChatCommandPlugin):
    """ Play the classic card game Cards Against Humanity """

    name = 'cah'

    priority = 1

    NUM_CARDS = 8 # size of a hand
    MIN_PLAYERS = 3 # number of players required to play
    TIME_ALLOWED = 180 # give players 3 minutes to play
    TIMES_TO_CHECK = 3 # issue up to 3 reminders

    long_desc = ('!j or !join - Joins the game.\n'
                 '!leave - Leaves the game.\n'
                 '!p or !play <card #> - Plays a card. May play more than one.\n'
                 '!winner <answer #> - Chooses a winner for a given prompt.'
                 '!mystatus - Shows information about yourself.')

    already_in = "[*] {0}, you are already a part of the game!"
    not_in = "[*] {0}, you are not a part of the game!"

    def setup(self, loader):
        super(CardsAgainstHumanity, self).setup(loader)

        self.db = loader.db # the database
        self.state = "join" # current state - join, play, or winner

        self.channel = "" # the channel where the game takes place
        # Try to load from the config, under cah.channel.
        if 'cah' in loader.config:
            self.channel = loader.config['cah'].get('channel', "")
        # Fall back on the first channel in the config.
        if self.channel == "":
            self.channel = loader.config['channels'][0]

        # State which carries over between rounds
        #

        # Cards and discard piles
        # Black cards are question cards
        # White cards are answers
        self.whites = []
        self.blacks = []
        self.black_discard = []
        self.white_discard = []

        self.players = defaultdict(list) # maps player name to their hand

        # State which is discarded between rounds
        #

        self.dealer = "" # name of player who is judging this round
        self.prompt = "" # current prompt card
        self.answers = defaultdict(list) # maps player name to played card(s)
        self.kick_votes = defaultdict(list) # maps player name to kick votes for that player

        # list of players who aren't the dealer
        # it is inefficient to build this list everytime we need it.
        self.avail_players = []

        self.player_queue = [] # people who will join the next round
        self.dealer_queue = [] # the order for dealing

        # Create the database tables, if necessary
        SQLAlchemyBase.metadata.create_all(self.db.engine)

        # Update the db
        self.flush_db()

        # Load the cards
        q = self.db.session.query(CardTable)
        self.whites = [x.desc for x in q.filter_by(color="white")]
        self.blacks = [x.desc for x in q.filter_by(color="black")]

        random.seed()

        random.shuffle(self.whites) # Erry' day I'm shufflin'!
        random.shuffle(self.blacks)

    def announce(self, bot, message):
        """Send a message to the game channel."""
        bot.msg(self.channel, message)

    def whisper(self, bot, user, message):
        """Send a message to a user in secret."""
        bot.notice(user, message)

    def remove_player(self, bot, comm, player):
        """Remove a player from the game, discarding their cards.

        If there aren't enough players, play is suspended until another player
        joins.  If everyone else has played, judging begins.  If the dealer
        leaves, a new round starts.
        """
        # Return cards to discard
        if player in self.players:
            self.white_discard += self.players[player]
        if player in self.answers:
            self.white_discard += self.answers[player]

        # Remove player
        if player in self.players:
            del self.players[player]
        if player in self.avail_players:
            self.avail_players.remove(player)
        if player in self.answers:
            del self.answers[player]
        if player in self.kick_votes:
            del self.kick_votes[player]
        if player in self.player_queue:
            self.player_queue.remove(player)
        if player in self.dealer_queue:
            self.dealer_queue.remove(player)

        if self.state == "play":
            if player == self.dealer:
                self.announce(bot, "[*] Game restarting... dealer left.")
                self.reset(bot, comm)

            if len(self.players) < self.MIN_PLAYERS:
                self.announce(bot,
                    "[*] There are fewer than 3 players playing now. "
                    "Waiting for more players...")
                self.reset(bot, comm)
            # Check to see if all cards are now submitted.
            elif len(self.answers) == len(self.avail_players):
                self.announce(bot, "[*] All players' cards are turned in.")
                random.shuffle(self.avail_players)
                self.show_answers(bot, comm)
                self.change_state(bot, comm, 'winner')

        elif self.state == "winner":
            if player == self.dealer:
                self.announce(bot, "[*] Game restarting... dealer left.")
                self.reset(bot, comm)

        self.announce(bot, self.current_players())

    def give_point(self, user):
        """Award a point to the user."""
        player_str = self.get_player_str()

        q = self.db.session.query(CAHTable).filter_by(game=player_str)
        winner = q.filter_by(user=user).first()
        if winner is None:
            winner = CAHTable(user, player_str)
        winner.score += 1
        self.db.session.add(winner)

        for player in self.players:
            if not q.filter_by(user=player).first():
                self.db.session.add(CAHTable(player, player_str))

        self.db.session.commit()

    def take_point(self, user):
        """Penalize the user one point."""
        player_str = self.get_player_str()
        q = self.db.session.query(CAHTable).filter_by(game=player_str)
        winner = q.filter_by(user=user).first()
        if winner is None or winner.score <= 0:
            return False
        winner.score -= 1
        self.db.session.add(winner)

        for player in self.players:
            if not q.filter_by(user=player).first():
                self.db.session.add(CAHTable(player, player_str))

        self.db.session.commit()
        return True

    def deal(self, user):
        """Refill a player's hand."""
        while len(self.players[user]) < self.NUM_CARDS:
            self.players[user].append(self.whites.pop(0))

    def reset(self, bot, comm):
        """End the current round. Does not award any points."""

        # Discard the prompt
        self.black_discard.append(self.prompt)

        # Discard the answers
        for p in self.players:
            self.white_discard += self.answers[p]

        self.prompt = ""
        self.answers.clear()
        self.kick_votes.clear()

        # Put discards back in deck
        if len(self.black_discard) > len(self.blacks) * 2:
            self.blacks += self.black_discard
            random.shuffle(self.blacks)
            self.black_discards = []

        if len(self.white_discard) > len(self.whites) * 2:
            self.whites += self.white_discard
            random.shuffle(self.whites)
            self.white_discards = []

        # Add queued players
        for p in self.player_queue:
            if p not in self.players:
                self.announce(bot, '{0} has joined the game!'.format(p))
            self.deal(p)
        del self.player_queue[:]

        # Start another round?
        if len(self.players) >= self.MIN_PLAYERS:
            self.prep_play(bot, comm)
        else:
            self.change_state(bot, comm, 'join')

    def start_afk_watcher(self, bot, comm, prompt, state, player, count=1):
        if (state == self.state and prompt == self.prompt
                and count < self.TIMES_TO_CHECK
                and player not in self.answers):
            say_for_state = {
                'play': 'Please play a card.',
                'winner': 'Please pick a winner.',
            }
            self.whisper(bot, player, say_for_state.get(state, 'Do something!'))
            reactor.callLater(self.TIME_ALLOWED/self.TIMES_TO_CHECK,
                              self.start_afk_watcher,
                              bot, comm, prompt, state, player,
                              count=count + 1)
        elif self.should_kick(player, prompt, state):
            self.announce(bot, '{0} has been kicked for taking too long.'.format(player))
            self.remove_player(bot, comm, player)

    def should_kick(self, player, prompt, state):
        still_playing = self.players.get(player)
        same_prompt = prompt == self.prompt

        if state != self.state:
            return False

        if not still_playing:
            return False

        # Check to see if the same hand is going on.
        if not same_prompt:
            return False

        if state == 'play':
            return not self.answers.get(player)

        elif state == 'winner':
            return player == self.dealer

        return False

    def change_state(self, bot, comm, state):
        """Set the state and start AFK timers."""
        self.state = state
        interval = self.TIME_ALLOWED/self.TIMES_TO_CHECK
        if state == 'play':
            for player in self.players:
                if player == self.dealer:
                    continue
                reactor.callLater(interval, self.start_afk_watcher,
                                  bot, comm, str(self.prompt), str(self.state),
                                  str(player))
        elif state == 'winner':
            reactor.callLater(interval, self.start_afk_watcher,
                              bot, comm, str(self.prompt), str(self.state),
                              str(self.dealer))

    def prep_play(self, bot, comm):
        """Set up the a new round"""

        # Select a dealer
        if not self.dealer_queue:
            self.dealer_queue += self.players
        self.dealer = self.dealer_queue.pop(0)
        self.avail_players = [p for p in self.players if p != self.dealer]

        # Draw a prompt card
        self.prompt = self.blacks.pop(0)

        self.announce(bot, "[*] {0} reads: {1}".format(self.dealer, self.prompt))
        self.announce(bot, '[*] Type: "!play <card #>" to fill blanks. Multiple '
                           'cards are played with "!play <card #> <card #>".')

        # Deal new cards to everybody
        for p in self.players:
            self.deal(p)

        # Show current hand to players
        for p in self.avail_players:
            self.show_hand(bot, p)

        self.change_state(bot, comm, 'play')


    def colorize(self, txt):
        if txt == BLANK:
            # Returns the light cyan color code
            return "\x0311" + txt + "\x03"
        # Returns the light green color code
        return "\x0309" + txt + "\x03"

    def get_player_str(self):
        return ' '.join(sorted(self.players.keys()))

    def show_top_scores(self, bot, comm, current_players=True):
        """Print a table of the top 5 scores.

        If current_players is True, show scores only for games between the
        current set of players.
        """

        if current_players:
            player_str = self.get_player_str()
            top = self.db.session.query(CAHTable).filter_by(game=player_str).order_by(
                    CAHTable.score.desc()).all()
        else:
            top = self.db.session.query(CAHTable).order_by(
                        CAHTable.score.desc().all())

        self.announce(bot, '{:^14} {:^14}'.format('User', 'Score'))
        self.announce(bot, '____________________________')
        for x in top[:5]:
            self.announce(bot, '{:^14}|{:^14}'.format(x.user, str(x.score)))

    def get_score(self, player):
        """Return a player's score in games between the current set of players."""
        player_str = self.get_player_str()
        player_obj = (self.db.session.query(CAHTable)
                        .filter_by(game=player_str)
                        .filter_by(user=player)
                        .first())

        if player_obj:
            return player_obj.score
        return 0

    def flush_db(self):
        """Clear out old cards, and bring in new ones."""
        self.db.session.query(CardTable).filter_by(official=True).delete()

        url = "http://web.engr.oregonstate.edu/~johnsdea/"

        whites_txt = urllib2.urlopen(url + "whites.txt").read().split("\n")
        blacks_txt = urllib2.urlopen(url + "blacks.txt").read().split("\n")
        new_whites = map(self.format_white, whites_txt)
        for white in new_whites:
            if white:
                self.db.session.add(CardTable(ude(white), "white", official=True))
        new_blacks = map(self.init_black, blacks_txt)
        for black in new_blacks:
            if black:
                self.db.session.add(CardTable(ude(black), "black", official=True))

        self.db.session.commit()

    def format_white(self, card):
        card = card.strip("\n")
        if card:
            if card.endswith("."):
                card = card[:-1]
        return card

    def init_black(self, card):
        card = card.strip("\n")
        if card:
            if BLANK not in card:
                card += " " + BLANK + "."
            return ''.join(map(self.colorize, re.split("(" + BLANK + ")", card)))
        return ''

    def format_black(self, card):
        # FIXME: This is quadratic
        for i in xrange(card.count(BLANK)):
            card = card.replace(BLANK, "{" + str(i) + "}", 1)
        return card

    def show_hand(self, bot, user):
        """Send a player a notice listing the cards in their hand."""

        cards = '. '.join(str(i + 1) + ": " + card
                          for i, card in enumerate(self.players[user]))

        self.whisper(bot, user, "Your hand is: [{0}]".format(cards))

    def show_answers(self, bot, comm):
        """Show a list of answers for judging."""

        for i, player in enumerate(self.avail_players):
            prompt = self.format_black(self.prompt)
            cards = prompt.format(*self.answers[player])
            self.announce(bot, "[*] [Answer #{0}]: {1}".format(i + 1, cards))

        self.announce(bot,
            '[*] {0}, please choose a winner with "!winner <answer #>".'
            .format(self.dealer))

    def current_players(self):
        """Returns a formatted list of the current players."""
        players = ', '.join(p for p in self.players) + '.'
        return "[*] Current players: " + players

    def queued_players(self):
        """Returns a formatted list of the queued players."""
        players = ', '.join(p for p in self.player_queue) + '.'
        return "[*] Queued Players: " + players

    class Join(Command):
        """ Join/Queue up for a game """

        regex = r'^(j|join) ?$'

        name = 'join'
        short_desc = '!j or !join - Joins the game.'
        long_desc = 'Joins the current Cards Against Humanity game.'

        def command(self, bot, comm, groups):
            if comm['pm']:
                return bot.reply(comm,
                    "Sorry, you can't join over PM. Join {0} to play."
                    .format(self.plugin.channel))
            elif comm['channel'] != self.plugin.channel:
                return bot.reply(comm,
                    "[*] Wrong channel. Join {0} to play."
                    .format(self.plugin.channel))

            user = comm['user']
            if user in self.plugin.players:
                return bot.reply(comm, self.plugin.already_in.format(user))
            elif user in self.plugin.player_queue:
                return bot.reply(comm, '[*] {0}, you are already in the queue!'.format(user))

            # This is only when the game is first starting.
            if self.plugin.state == "join":
                self.plugin.deal(user)
                if len(self.plugin.players) >= self.plugin.MIN_PLAYERS:
                    self.plugin.announce(bot,
                        "[*] {0} has joined the game! "
                        "There are now enough players to play!".format(user))
                    self.plugin.prep_play(bot, comm)
                else:
                    self.plugin.announce(bot,
                        "[*] {0} has joined the game! "
                        "Waiting for {1} more player(s)."
                        .format(user, 3 - len(self.plugin.players)))
            else:
                self.plugin.announce(bot, "[*] {0} has joined the queue!".format(user))
                self.plugin.player_queue.append(user)
            self.plugin.announce(bot, self.plugin.current_players())

    class Leave(Command):
        name = 'leave'
        regex = r'^leave ?$'

        short_desc = '!leave - Leaves the game.'
        long_desc = 'Leaves the current Cards Against Humanity game.'

        def command(self, bot, comm, groups):
            user = comm['user']

            if (user not in self.plugin.players and
                    user not in self.plugin.player_queue):
                return bot.reply(comm, self.plugin.not_in.format(user))

            self.plugin.announce(bot, "[*] {0} has left the game!".format(user))
            self.plugin.remove_player(bot, comm, user)

    class Play(Command):
        name = 'play'
        regex = r'^(p|play) (.*)'

        short_desc = '!p or !play - Plays a card from your hand.'
        long_desc = ('Play a card from your card with "!play <card #>".'
                     '!play random - plays a random card.'
                     'Multiple cards may be played with "!play <card #> '
                     '<card #>".')

        def command(self, bot, comm, groups):
            user = comm['user']
            if user not in self.plugin.players:
                return bot.reply(comm, self.plugin.not_in.format(user))
            elif user == self.plugin.dealer:
                return bot.reply(comm, "[*] {0}, you are the dealer!".format(user))

            # Try to parse the list of indices
            blanks = self.plugin.prompt.count(BLANK)
            indices = []
            if not groups[1]:
                return bot.reply(comm,
                    "[*] {0}, you didn't provide hand index(s) for cards!".format(user))
            elif groups[1] == 'random':
                hand = self.plugin.players[user]
                indices = random.sample(xrange(len(hand)), blanks)
            else:
                try:
                    indices = map(int, groups[1].split(" "))
                except (IndexError, ValueError):
                    return bot.reply(comm,
                        "[*] {0}, that doesn't look like index(s) for cards.".format(user))

                if len(indices) != blanks:
                    return bot.reply(comm,
                        "[*] {0}, you didn't provide the correct amount of cards!".format(user))

                if not all(1 <= n <= len(self.plugin.players[user]) for n in indices):
                    return bot.reply(comm,
                        "[*] {0}, you don't have card(s) with those index(s).".format(user))

            # If the player has already played, take the previous answers back
            if user in self.plugin.answers:
                self.plugin.players[user] += self.plugin.answers[user]
                del self.plugin.answers[user]

            # Add the played cards to the answer pile
            self.plugin.answers[user] = [
                self.plugin.players[user][index - 1]
                for index in indices
            ]

            # Remove them from the player's hand
            # but be careful about it
            for index in reversed(sorted(indices)):
                self.plugin.players[user].pop(index - 1)

            # If everybody has played, start judging
            if len(self.plugin.answers) == len(self.plugin.avail_players):
                self.plugin.announce(bot, "[*] All players have turned in their cards.")
                random.shuffle(self.plugin.avail_players)
                self.plugin.show_answers(bot, comm)
                self.plugin.change_state(bot, comm, 'winner')

    class Winner(Command):
        name = 'winner'
        regex = r'^(w|winner) (.*)'

        short_desc = '!winner - Chooses a winner.'
        long_desc = ('Choose a winner from the available options with "!winner'
                     ' <card #>".')

        def command(self, bot, comm, groups):
            user = comm['user']
            if self.plugin.state != "winner":
                return bot.reply(comm,
                    "[*] {0}, it is not time to choose a winner!".format(user))
            elif user != self.plugin.dealer:
                return bot.reply(comm,
                    "[*] {0}, you may not choose the winner!".format(user))

            try:
                winner_ind = int(groups[1])
            except ValueError:
                return bot.reply(comm, "[*] {0}, that is not a valid winner!".format(user))

            if not 1 <= winner_ind <= len(self.plugin.answers):
                return bot.reply(comm, "[*] {0}, that answer doesn't exist!".format(user))

            winner = self.plugin.avail_players[winner_ind - 1]
            self.plugin.announce(bot, "[*] {0}, you won this round! Congrats!".format(winner))

            self.plugin.give_point(winner)
            self.plugin.show_top_scores(bot, comm)
            self.plugin.reset(bot, comm)

    class MyStatus(Command):
        name = 'mystatus'
        regex = r'^mystatus'

        short_desc = '!mystatus - Shows information about yourself.'
        long_desc = 'Shows information about yourself regarding the game.'

        def command(self, bot, comm, groups):
            user = comm['user']
            msg = ["{0}", "Score: {0}", "Playing: {0}", "Dealer: {0}",
                    "Hand: [{0}]"]

            score = 0
            player = self.plugin.db.session.query(CAHTable).filter_by(user=user).first()
            if player:
                score = player.score

            playing = "Yes" if user in self.plugin.players else "No"
            dealer = "Yes" if user == self.plugin.dealer else "No"
            hand = "None"
            if user in self.plugin.players:
                hand = '. '.join(str(i + 1) + ": " + card
                    for i, card in enumerate(self.plugin.players[user]))

            # Since we can't print new lines...
            msgs = zip(msg, [user, score, playing, dealer, hand])
            for msg, value in msgs:
                self.plugin.whisper(bot, user, msg.format(value))

    class Players(Command):
        name = 'players'
        regex = r'^players'

        short_desc = '!players - Shows the current players.'
        long_desc = 'Shows players playing, and in the queue.'

        def command(self, bot, comm, groups):
            bot.reply(comm, self.plugin.current_players())
            bot.reply(comm, self.plugin.queued_players())

    class Kick(Command):
        name = 'kick'
        regex = r'^kick (.+)'

        short_desc = '!kick <player_name> Cast a vote to kick a player.'
        long_desc = ('Vote to kick a player. If 70% or more of the current '
                     'players vote to kick a player, they should be kicked.')

        def command(self, bot, comm, groups):
            user = comm['user']
            target = groups[0]

            if not self.plugin.players.get(target):
                return bot.reply(comm, "[*] Player '{0}' doesn't exist...".format(target))
            elif user in self.plugin.kick_votes[target]:
                return bot.reply(comm, "[*] You already voted to kick this player!")
            elif user == target:
                return bot.reply(comm, "[*] You can't kick yourself!")

            self.plugin.kick_votes[target].append(user)
            num_votes = len(self.plugin.kick_votes[target])
            num_voters = len(self.plugin.players) - 1

            # if 70% or more of voting players want to kick a player, they are
            # kicked
            if ((num_votes/float(num_voters)) * 100 ) > 70:
                self.plugin.announce(bot, "{0} has been kicked from the game!".format(target))
                self.plugin.remove_player(bot, comm, target)


    class Hand(Command):
        name = 'hand'
        regex = r'^hand'

        short_desc = '!hand - Shows your current hand.'

        def command(self, bot, comm, groups):
            self.plugin.show_hand(bot, comm['user'])

    class AddCard(Command):
        name = 'addcard'
        regex = r'^addcard \"(.+)\" \"?(.+)\"?$'

        short_desc = '!addcard - Adds a card to the deck.'
        long_desc = ('!addcard "Description/Text of card" "color".'
                     'To indicate the blanks for a black card, use one "_".')

        def command(self, bot, comm, groups):
            desc = groups[0]
            color = groups[1].replace('"', '')

            if color not in ['white', 'black']:
                return bot.reply(comm,
                    "[*] {0}, that color card doesn't exist!".format(user))

            elif color == 'black':
                underscore_re = re.compile('(_+)+')
                formatted, num_replacements = underscore_re.subn(BLANK, desc)

                if num_replacements == 0 or num_replacements > 3:
                    return bot.reply(comm, "[*] You provided too few or many blanks!")

                desc = ude(self.plugin.init_black(formatted))
                self.plugin.black_discard.append(desc)

            elif color == 'white':
                desc = ude(self.plugin.format_white(desc))
                self.plugin.white_discard.append(desc)

            new_card = CardTable(desc=desc, color=color, official=False)
            self.plugin.db.session.add(new_card)
            self.plugin.db.session.commit()

            return bot.reply(comm,
                '[*] Card: {0} Color: {1} added to db!'.format( desc, color))

    class Poke(Command):
        name = 'poke'
        regex = r'^poke (.+)'

        short_desc = '!poke - Pokes a player with a short reminder.'

        def command(self, bot, comm, groups):
            # Get rid of trailing whitespace.
            target = groups[0].strip()

            if target not in self.plugin.players:
                return bot.reply(comm, '[*] That player is not playing right now!')

            elif target == comm['user']:
                return bot.reply(comm, '[*] Why are you poking yourself?')

            elif target == self.plugin.dealer:
                if self.plugin.state != 'winner':
                    return bot.reply(comm,
                        "[*] The dealer doesn't need to do anything right now.")
                self.plugin.show_answers(bot, comm)

            else:
                if self.plugin.state != 'play':
                    return bot.reply(comm, '[*] Players do not need to do anything right now.')

                if target in self.plugin.answers:
                    return bot.reply(comm, '[*] {0} has already played their cards!'.format(target))

                self.plugin.whisper(bot, target, '[*] Please play a card.')
                self.plugin.show_hand(bot, target)

    class Redraw(Command):
        name = 'redraw'
        regex = r'^redraw (.+)'

        short_desc = '!redraw - Provide indices to redraw for. Cost: 1 point.'

        def command(self, bot, comm, groups):
            user = comm['user']
            if user not in self.plugin.players:
                return bot.reply(comm, self.plugin.not_in.format(user))

            if not self.plugin.take_point(user):
                return bot.reply(comm, "[*] You don't have enough points to do that.")

            indices = set(map(int, groups[0].split(" ")))

            # Don't change index of cards that are being removed..
            for index in reversed(sorted(indices)):
                exchange = self.plugin.players[user].pop(index - 1)
                self.plugin.white_discard.append(exchange)

            self.plugin.deal(user)
            self.plugin.whisper(bot, user, 'Exchanged {0} card{1}.'.format(len(indices),
                        (len(indices) > 1) * 's'))
            self.plugin.show_hand(bot, user)

    class GameStatus(Command):
        name = 'gamestatus'
        regex = r'^gamestatus'

        short_desc = '!gamestatus - Sends you stats about the current game.'

        def command(self, bot, comm, groups):
            responses = []
            responses.append('    Player    |    Played    |    Dealer    |    Score')
            responses.append('______________|______________|______________|____________')
            stats = '{:^14} {:^14} {:^14} {:^14}'
            player_scores = [(self.plugin.get_score(p), p) for p in self.plugin.players]
            player_scores.sort(reverse=True)
            for score, player in player_scores:
                # The str(bool) is a hack around formating in python kinda sucking.
                resp = stats.format(player,
                                    str(bool(self.plugin.answers.get(player))),
                                    str(player == self.plugin.dealer),
                                    score)
                responses.append(resp)

            for resp in responses:
                self.plugin.whisper(bot, comm['user'], resp)


class CardTable(SQLAlchemyBase):
    """
    This is only for persistant storage of all cards. More can also be
    added through commands in this manner.
    """

    __tablename__ = 'cah_cards'

    id = Column(Integer, primary_key=True)
    desc = Column(String)
    color = Column(String)
    official = Column(Boolean)

    def __init__(self, desc, color, official=True):
        self.desc = desc
        self.color = color
        self.official = official

    def __repr__(self):
        return self.desc


class CAHTable(SQLAlchemyBase):
    """
    For storing scores on a per player per game basis.
    """

    __tablename__ = 'cah'

    id = Column(Integer, primary_key=True)
    game = Column(String)
    user = Column(String)
    score = Column(Integer)

    def __init__(self, user, game, score=0):
        self.user = user
        self.game = game
        self.score = score

    def __repr__(self):
        return "%s: %d" % self.user, self.score
