from rgbmatrix import graphics
from PIL import Image, ImageFont, ImageDraw, ImageSequence
from utils import center_text
from renderer.screen_config import screenConfig
import time as t
import debug
from pprint import pprint
import math

# Cap each nap so a flexed or rescheduled game is still picked up
STANDBY_MAX_NAP = 21600
# Seconds per pixel of name scrolling, and blank pixels between repeats
SCROLL_STEP = 0.06
SCROLL_GAP = 12
# Seconds to show the abbreviation after each full scroll
SCROLL_PAUSE = 6
# Other league games: seconds on your matchup between looks at them, how long
# the list stays up, and how long it flashes up when one of those scores changes
LEAGUE_EVERY = 30
LEAGUE_SHOW = 8
LEAGUE_FLASH = 5
# The next NFL game, when it has its kickoff slot to itself: seconds between
# looks at it and how long it stays up
NEXT_GAME_EVERY = 60
NEXT_GAME_SHOW = 6
# Live NFL games with our starters: seconds between looks and each game's time up
LIVE_GAMES_EVERY = 40
LIVE_GAME_SHOW = 5
# While another screen is up, seconds between checks on our fantasy score
SCORE_CHECK_EVERY = 4
# An MLB team to show while it's playing: seconds between looks and how long it stays up
MLB_TEAM = 'PHI'
MLB_EVERY = 45
MLB_SHOW = 8
# How often to ask ESPN whether the week's last game is over, and the flash then
WEEK_CHECK_EVERY = 60
WEEK_OVER_FLASHES = 5
# Standby panel brightness (1-100, 1 is the dimmest), and minutes the scoreboard
# stays up after the last game with one of our starters stops
STANDBY_BRIGHTNESS = 1
LINGER_MINUTES = 10


class MainRenderer:
    def __init__(self, matrix, data):
        self.matrix = matrix
        self.data = data
        self.screen_config = screenConfig("64x32_config")
        self.canvas = matrix.CreateFrameCanvas()
        self.width = 64
        self.height = 32
        self.avsize = 19
        # use this to check if week has changed
        self.week = data.week
        self._in_standby = False
        self._brightness = matrix.brightness
        self._idle_since = None
        self._idle_checked_at = 0
        # Whether a game with one of our matchup's starters is on, and the next kickoff if not
        self._live_checked_at = 0
        self._starters_on = True
        self._next_kickoff = None
        # Create a new data image.
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)
        # Load the fonts
        self.font = ImageFont.truetype("fonts/score_large.otf", 16)
        self.font_mini = ImageFont.truetype("fonts/04B_24__.TTF", 8)
        self.font_vs = ImageFont.truetype("fonts/CG pixel 3x5.ttf", 10)
        self.font_res = ImageFont.truetype("fonts/CG pixel 3x5.ttf", 6)
        # Live-view scores, small enough to fit under the logos (score_large blurs this small)
        self.font_score = ImageFont.truetype("fonts/04B_03B_.TTF", 8)
        # Names too wide for their box, animated by _hold()
        self._scrollers = []
        self._scroll_tick = 0
        self._frame = None
        # Other league games' last seen scores, keyed by team, and when the list was last up
        self._league_scores = {}
        self._league_changed = set()  # changes not yet shown
        self._league_shown_at = t.time()
        self._next_game_shown_at = t.time()
        self._mlb_shown_at = t.time()
        self._live_games_shown_at = t.time()
        # Our matchup's scores as last put on screen, to spot a change behind other screens
        self._shown_scores = None
        self._team_logos = {}  # (league, abbr) -> logo image, or None if there isn't one
        # Week in which a game was seen unfinished; cleared once its end is celebrated
        self._games_live_week = None
        self._week_checked_at = 0

    @staticmethod
    def _display_name(matchup, side):
        # Team name first; the owner only when the platform gave no team name
        return (matchup.get(side + '_team') or matchup.get(side + '_name') or '').strip()

    @staticmethod
    def _abbreviate(name):
        # "Bill's Mafia" -> "BM", "FracturedButWhole" -> "FBW"
        caps = ''.join(c for c in name if c.isupper())
        if len(caps) >= 2:
            return caps
        words = name.replace('-', ' ').replace('_', ' ').split()
        if len(words) >= 2:
            return ''.join(w[0] for w in words).upper()
        return name[:3].upper()

    def _place_text(self, text, font, x, y, width, align='left', centre_x=None):
        """Draw text in a box of the given width, or queue it to scroll if it won't fit.

        A scrolling name pauses on its abbreviation after each pass, centred on
        `centre_x` (default: the middle of the box).
        """
        text_w = font.getbbox(text)[2] if text else 0
        if text_w <= width:
            dx = width - text_w if align == 'right' else 0
            self.draw.text((x + dx, y), text, fill=(255, 255, 255), font=font)
            return
        height = font.getbbox(text)[3]
        strip = Image.new('RGB', (text_w + SCROLL_GAP, height))
        ImageDraw.Draw(strip).text((0, 0), text, fill=(255, 255, 255), font=font)
        abbr = self._abbreviate(text)
        while len(abbr) > 1 and font.getbbox(abbr)[2] > width:
            abbr = abbr[:-1]
        abbr_w = font.getbbox(abbr)[2]
        centre = (centre_x - x) if centre_x is not None else width // 2
        abbr_x = min(max(centre - abbr_w // 2, 0), width - abbr_w)
        still = Image.new('RGB', (width, height))
        ImageDraw.Draw(still).text((abbr_x, 0), abbr, fill=(255, 255, 255), font=font)
        self._scrollers.append((strip, still, x, y, width))

    def _note_league_changes(self, games):
        """Remember which teams in the other league games have scored since the list was last up."""
        for game in games:
            for side in game:
                last = self._league_scores.get(side['key'])
                if last is not None and last != side['score']:
                    self._league_changed.add(side['key'])
                self._league_scores[side['key']] = side['score']

    def _draw_league(self, games, seconds):
        """List the other league games, one per row, paging if they don't fit."""
        rows = 5
        pages = [games[i:i + rows] for i in range(0, len(games), rows)] or [[]]
        for page in pages:
            self.image = Image.new('RGB', (self.width, self.height))
            self.draw = ImageDraw.Draw(self.image)
            for row, (left, right) in enumerate(page):
                y = row * 6 + 1
                for side, other, is_left in ((left, right, True), (right, left, False)):
                    if side['key'] in self._league_changed:
                        colour = (165, 200, 50)
                    elif side['score'] < other['score']:
                        colour = (110, 110, 110)  # trailing
                    else:
                        colour = (255, 255, 255)
                    score = str(int(side['score']))
                    score_w = self.font_mini.getbbox(score)[2]
                    # Scores meet in the middle, names hug the edges
                    score_x = 31 - score_w if is_left else 34
                    room = score_x - 2 if is_left else self.width - (score_x + score_w + 2)
                    abbr = self._abbreviate(side['name'])
                    while len(abbr) > 1 and self.font_mini.getbbox(abbr)[2] > room:
                        abbr = abbr[:-1]
                    abbr_x = 0 if is_left else self.width - self.font_mini.getbbox(abbr)[2]
                    self.draw.text((score_x, y), score, fill=colour, font=self.font_mini)
                    self.draw.text((abbr_x, y), abbr, fill=colour, font=self.font_mini)
            self._frame = self.image
            self._show(self._frame)
            interrupted = self._hold_away(seconds / len(pages))
            if interrupted:
                break
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)
        self._league_changed = set()
        self._league_shown_at = t.time()
        return interrupted

    def _team_logo(self, league, abbr):
        """An ESPN team logo on black at logo size, or None if there isn't one."""
        key = (league, abbr)
        if key not in self._team_logos:
            path = self.data.team_logo(league, abbr)
            logo = None
            if path:
                try:
                    art = Image.open(path).convert('RGBA')
                    # Trim ESPN's transparent padding so the logo fills its box
                    art = art.crop(art.getbbox() or (0, 0) + art.size)
                    art.thumbnail((self.avsize, self.avsize), Image.LANCZOS)
                    logo = Image.new('RGB', (self.avsize, self.avsize))
                    logo.paste(art, ((self.avsize - art.width) // 2,
                                     (self.avsize - art.height) // 2), art)
                except Exception as error:
                    debug.warning('could not load {0} logo: {1}'.format(abbr, error))
            self._team_logos[key] = logo
        return self._team_logos[key]

    def _draw_team_logos(self, league, away, home, y):
        """Away logo on the left, home on the right, or their codes if there's no logo."""
        for abbr, x in ((away, 0), (home, self.width - self.avsize)):
            logo = self._team_logo(league, abbr)
            if logo is not None:
                self.image.paste(logo, (x, y))
            else:
                text_w = self.font_vs.getbbox(abbr)[2]
                self.draw.text((x + (self.avsize - text_w) // 2, y + 6), abbr,
                               fill=(255, 255, 255), font=self.font_vs)

    def _centre(self, text, y, colour, font=None):
        font = font or self.font_mini
        self.draw.text((center_text(font.getbbox(text)[2], 32), y), text, fill=colour, font=font)

    def _new_screen(self):
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)

    def _draw_next_game(self, game, seconds):
        """Show the next NFL game: kickoff, logos, our starters in it, spread and over/under."""
        self._new_screen()
        grey = (110, 110, 110)
        # Same frame as the live view: text row on top, logos at y=6, text row at y=25
        self._centre(self._kickoff_label(game['kickoff']), 0, grey)
        self._draw_team_logos('nfl', game['away'], game['home'], 6)
        self._centre('VS' if game['neutral'] else 'AT', 7, grey)
        # How many of each side's starters play in it, once either has any
        if game['user_starters'] or game['opp_starters']:
            self._centre('ME {0}'.format(game['user_starters']), 13, (165, 200, 50))
            self._centre('OPP {0}'.format(game['opp_starters']), 19, (255, 44, 44))
        spread = game['spread'] or 'NO LINE'
        self.draw.text((0, 25), spread, fill=(255, 165, 0), font=self.font_mini)
        if game['over_under'] is not None:
            total = '{0:g}'.format(game['over_under'])
            # Drop the "O/U" label rather than crowd a long spread
            for text in ('O/U ' + total, 'O' + total):
                text_w = self.font_mini.getbbox(text)[2]
                if self.font_mini.getbbox(spread)[2] + 3 + text_w <= self.width:
                    self.draw.text((self.width - text_w, 25), text, fill=grey, font=self.font_mini)
                    break
        return self._put_up(seconds)

    def _put_up(self, seconds):
        """Show the screen just drawn for `seconds`, or less if our fantasy score moves."""
        self._frame = self.image
        self._show(self._frame)
        interrupted = self._hold_away(seconds)
        self._new_screen()
        return interrupted

    def _draw_logo_scores(self, away_score, home_score):
        # Scores under their logos; the trailing side greyed like the league list
        for score, other, x in ((away_score, home_score, 0),
                                (home_score, away_score, self.width - self.avsize)):
            text = str(score)
            colour = (110, 110, 110) if score < other else (255, 255, 255)
            text_w = self.font_score.getbbox(text)[2]
            self.draw.text((x + (self.avsize - text_w) // 2, 25), text, fill=colour, font=self.font_score)

    @staticmethod
    def _game_clock(detail):
        # ESPN's "15:00 - 2nd" -> "Q2 15:00", "End of 1st" -> "END Q1", "Halftime" -> "HALF"
        quarters = {'1st': 'Q1', '2nd': 'Q2', '3rd': 'Q3', '4th': 'Q4'}
        if ' - ' in detail:
            clock, period = detail.split(' - ', 1)
            return '{0} {1}'.format(quarters.get(period, period.upper()), clock)
        if detail.startswith('End of '):
            period = detail[len('End of '):]
            return 'END ' + quarters.get(period, period.upper())
        return 'HALF' if detail == 'Halftime' else detail.upper()

    def _draw_live_games(self, games, seconds):
        """Show each live NFL game with our starters: clock, logos, score, who has the ball."""
        for game in games:
            self._new_screen()
            self._centre(self._game_clock(game['clock']), 0, (255, 255, 255))
            self._draw_team_logos('nfl', game['away'], game['home'], 6)
            if game['user_starters'] or game['opp_starters']:
                self._centre('ME {0}'.format(game['user_starters']), 10, (165, 200, 50))
                self._centre('OPP {0}'.format(game['opp_starters']), 17, (255, 44, 44))
            self._draw_logo_scores(game['away_score'], game['home_score'])
            # A dot beside the score of the side with the ball
            if game['possession'] == 'away':
                self.draw.rectangle((21, 27, 22, 28), fill=(255, 165, 0))
            elif game['possession'] == 'home':
                self.draw.rectangle((41, 27, 42, 28), fill=(255, 165, 0))
            if self._put_up(seconds):
                return True
        return False

    def _draw_mlb_game(self, game, seconds):
        """Show a live MLB game: inning, logos, runners on base, outs and the score."""
        self._new_screen()
        # "Bot 7th" -> "BOT 7"
        words = game['inning'].split()
        inning = game['inning'].upper()
        if len(words) == 2 and words[1][:-2].isdigit():
            inning = '{0} {1}'.format(words[0].upper(), words[1][:-2])
        self._centre(inning, 0, (255, 255, 255))
        self._draw_team_logos('mlb', game['away'], game['home'], 6)
        # Bases as a diamond: second on top, first to the right, third to the left
        lit, unlit = (255, 215, 0), (60, 60, 60)
        for on, (x, y) in zip(game['bases'], ((36, 13), (31, 8), (26, 13))):
            self.draw.rectangle((x, y, x + 2, y + 2), fill=lit if on else unlit)
        if game['outs'] is not None:
            for i in range(3):
                x = 26 + i * 5
                self.draw.rectangle((x, 20, x + 1, 21),
                                    fill=(255, 44, 44) if i < game['outs'] else unlit)
        self._draw_logo_scores(game['away_score'], game['home_score'])
        return self._put_up(seconds)

    def _check_week_over(self):
        """Flash the screen once when the week's last NFL game goes final."""
        if t.time() - self._week_checked_at < WEEK_CHECK_EVERY:
            return
        self._week_checked_at = t.time()
        finished = self.data.week_finished()
        if finished is None:
            return
        if not finished:
            self._games_live_week = self.data.week
        elif self._games_live_week == self.data.week:
            # Only after seeing it unfinished, so a restart mid-week doesn't flash
            self._games_live_week = None
            debug.info('Last game of week {0} is over'.format(self.data.week))
            self._flash(WEEK_OVER_FLASHES)

    def _games_over(self):
        """True once no game with one of our starters has been on for LINGER_MINUTES."""
        if not self.data.config.sleep_enabled or t.time() - self._idle_checked_at < WEEK_CHECK_EVERY:
            return False
        self._idle_checked_at = t.time()
        upcoming = self.data.next_kickoff()
        # On now, schedule unknown, or due soon enough that standby wouldn't take over
        if upcoming is None or upcoming[0] < self.data.config.wake_before_hours * 3600:
            self._idle_since = None
            return False
        if self._idle_since is None:
            self._idle_since = t.time()
        return t.time() - self._idle_since >= LINGER_MINUTES * 60

    def _matchup_live(self):
        """False while no game with a starter from either side of our matchup is on."""
        if t.time() - self._live_checked_at >= WEEK_CHECK_EVERY:
            self._live_checked_at = t.time()
            upcoming = self.data.next_kickoff()
            # Unknown schedule: show the matchup rather than hide it by mistake
            self._starters_on = upcoming is None or upcoming[0] == 0
            self._next_kickoff = None if upcoming is None else upcoming[1]
        return self._starters_on

    def _flash(self, times):
        # Blink the current screen, with any scrolling names frozen on their initials
        frame = self._frame.copy()
        for strip, still, x, y, width in self._scrollers:
            frame.paste(still, (x, y), still.convert('L'))
        blank = Image.new('RGB', (self.width, self.height))
        for _ in range(times):
            self._show(blank)
            t.sleep(0.25)
            self._show(frame)
            t.sleep(0.4)

    def _show(self, image):
        self.canvas.SetImage(image, 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def _fantasy_changed(self):
        """Re-read our fantasy scores; True if either moved since our matchup was last up."""
        if self._shown_scores is None or not self.data.check_scores:
            return False
        try:
            self.data.refresh_scores()
        except Exception as error:
            debug.warning('could not refresh scores: {0}'.format(error))
            return False
        matchup = self.data.matchup
        return bool(matchup) and (matchup['user_score'], matchup['opp_score']) != self._shown_scores

    def _hold_away(self, seconds):
        """Hold a screen other than our matchup, cutting it short (True) if our score moves."""
        end = t.time() + seconds
        while t.time() < end:
            t.sleep(max(0, min(SCORE_CHECK_EVERY, end - t.time())))
            if t.time() < end and self._fantasy_changed():
                debug.info('Fantasy score changed, back to our matchup')
                return True
        return False

    def _hold(self, seconds):
        """Keep the last frame up for `seconds`, scrolling any names that didn't fit."""
        if not self._scrollers or self._frame is None:
            self._scrollers = []
            t.sleep(seconds)
            return
        # All names share one cycle sized to the longest: a shorter name starts
        # its pass later so every pass ends together, then all hold on initials
        longest = max(strip.width for strip, *_ in self._scrollers)
        cycle = longest + int(SCROLL_PAUSE / SCROLL_STEP)
        end = t.time() + seconds
        while t.time() < end:
            frame = self._frame.copy()
            phase = self._scroll_tick % cycle
            for strip, still, x, y, width in self._scrollers:
                offset = phase - (longest - strip.width)
                if not 0 <= offset < strip.width:
                    window = still
                else:
                    window = Image.new('RGB', (width, strip.height))
                    window.paste(strip, (-offset, 0))
                    window.paste(strip, (strip.width - offset, 0))
                # Mask on lit pixels so the window never blanks what's underneath
                frame.paste(window, (x, y), window.convert('L'))
            self._show(frame)
            self._scroll_tick += 1
            t.sleep(SCROLL_STEP)
        self._scrollers = []

    def render(self):
        while True:
            if self.week > 0 and self.week < 19:
                debug.info('render game')
                self.__render_game()
            # weeks 18+, off season
            else:
                debug.info('Off season state')
                self.__render_off_season()

    @staticmethod
    def _kickoff_label(kickoff):
        # "THU 8:15PM", in the Pi's local time
        local = kickoff.astimezone()
        return '{0} {1}:{2:02d}{3}'.format(
            local.strftime('%a').upper(),
            local.hour % 12 or 12,
            local.minute,
            'AM' if local.hour < 12 else 'PM')

    def _paint_next_kickoff(self, kickoff):
        label = 'NEXT: ' + self._kickoff_label(kickoff)
        self._new_screen()
        pos = center_text(self.font_mini.getbbox(label)[2], 32)
        # Full white: the panel brightness does the dimming, and grey at 1% vanishes
        self.draw.multiline_text((pos, 12), label, fill=(
            255, 255, 255), font=self.font_mini, align="center")

    def _draw_standby(self, kickoff):
        self._paint_next_kickoff(kickoff)
        self._show(self.image)

    def _wake(self):
        if self._in_standby:
            self.matrix.brightness = self._brightness
        self._in_standby = False
        return False

    def _standby(self):
        # Dim and wait whenever no game with one of our matchup's starters is on
        # or due within wake_before_hours
        config = self.data.config
        if not config.sleep_enabled:
            return self._wake()
        upcoming = self.data.next_kickoff()
        # Unknown schedule: stay awake rather than risk sleeping through a game
        if upcoming is None:
            return self._wake()
        seconds, kickoff = upcoming
        nap = min(seconds - config.wake_before_hours * 3600, STANDBY_MAX_NAP)
        if nap <= 0:
            return self._wake()
        if not self._in_standby:
            # Brightness applies as pixels are drawn, so set it before drawing
            self.matrix.brightness = STANDBY_BRIGHTNESS
        self._in_standby = True
        debug.info('Standby, next kickoff in {0:.1f}h, sleeping {1:.1f}h'.format(
            seconds / 3600, nap / 3600))
        self._draw_standby(kickoff)
        t.sleep(nap)
        # A nap can outlast the current week
        self.data.refresh_week()
        self.week = self.data.week
        return True

    # TODO: figure out a more programmatic way of handling this in refactor
    def __render_game(self):
        if self._standby():
            return
        debug.info('ping render_game')
        time = self.data.get_current_date()
        # for the days after preseason ends, but there's still a lot of time before the season starts
        if self.data.get_season_type() == 'kickoff':
            debug.info('Pre-Kickoff State, waiting 6 hours')
            self._draw_pregame()
            self._hold(21600)
        # check if thursday and before 16h00 UTC (fixed for US Thanksgiving games)
        elif time.weekday() == 3 and 9 <= time.hour <= 15 and time.minute <= 59:
            debug.info('Pre-Game State, waiting 15 min')
            self._draw_pregame()
            self._hold(900)
        # thursday before 17h00 UTC
        elif time.weekday() == 3 and time.hour == 16 and time.minute <= 29:
            debug.info('Pre-Game State, waiting 1 minute')
            self._draw_pregame()
            self._hold(60)
        # After Monday night game has ended (Tuesday morning)
        elif time.weekday() == 1 and time.hour >= 9:
            debug.info('Final State, waiting 6 hours')
            self._draw_post_game()
            # sleep 6 hours
            t.sleep(21600)
        # friday after 00h15 UTC until tuesday 06h00 UTC
        else:
            debug.log('Live State, checking every 10s')
            # Draw the current game
            self._draw_game()

    def __render_off_season(self):
        debug.log('ping_off_season')
        self._draw_off_season()
        t.sleep(86400)  # sleep 24 hours

    # need to keep working on this
    def _draw_pregame(self):
        # get the matchup
        # get the matchup pics and resize them to 32x32
        # don't you love how messy this is? boy
        if self.data.matchup:
            matchup = self.data.matchup
            opp_av = matchup['opp_av']
            user_av = matchup['user_av']
            if opp_av is None:
                opp_av = 'noneLogo.png'
            if user_av is None:
                user_av = 'noneLogo.png'
            week = self.data.week
            game_date = 'WEEK {}'.format(week)
            vs = 'VS'
            _bbox = self.font_mini.getbbox(game_date)
            _width = _bbox[2] - _bbox[0]
            game_date_pos = center_text(_width, 32)
            vs_bbox = self.font_vs.getbbox(vs)
            vs_pos = center_text(vs_bbox[2] - vs_bbox[0], 32)
            self.draw.text(
                (game_date_pos, 7),  # ensure no clipping of tall glyphs like '6'
                game_date,
                fill=(255, 255, 255),
                font=self.font_mini,
                align="center"
            )
            self.draw.multiline_text(
                (vs_pos + 1, 14), vs, fill=(255, 255, 255), font=self.font_vs, align="center")
            # Each name gets half of the top row, clear of the WEEK label and logos
            opp_name = self._display_name(matchup, 'opp')
            user_name = self._display_name(matchup, 'user')
            debug.log("[pregame] display user='%s' opp='%s'" % (user_name, opp_name))
            # Paused initials centre over the logos (19px wide at x=0 and x=45)
            self._place_text(opp_name, self.font_mini, 0, 1, 30, centre_x=9)
            self._place_text(user_name, self.font_mini, 34, 1, 30, align='right', centre_x=54)
            if self.data.platform == "yahoo":
                # Open the logo image file
                opp_logo = Image.open(
                    'logos/{}.jpg'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}.jpg'.format(user_av)).resize((19, 19), Image.BOX)
            elif self.data.platform == "espn":
                opp_logo = Image.open(
                    'logos/{}'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}'.format(user_av)).resize((19, 19), Image.BOX)
            else:
                # try png for sleeper
                opp_logo = Image.open(
                    'logos/{}.png'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}.png'.format(user_av)).resize((19, 19), Image.BOX)
            # Composite the logos into the frame so _hold() can redraw it
            self.image.paste(opp_logo.convert("RGB"), (0, 13))
            self.image.paste(user_logo.convert("RGB"), (45, 7))
            self._frame = self.image
            self._show(self._frame)
            # Refresh the Data image.
            self.image = Image.new('RGB', (self.width, self.height))
            self.draw = ImageDraw.Draw(self.image)
        else:
            # (Need to make the screen run on it's own) If connection to the API fails, show bottom red line and refresh in 1 min.
            self.draw.line((0, 0) + (self.width, 0), fill=128)
            self.canvas = self.matrix.SwapOnVSync(self.canvas)
            t.sleep(60)  # sleep for 1 min
            # Refresh canvas
            self.image = Image.new('RGB', (self.width, self.height))
            self.draw = ImageDraw.Draw(self.image)

    def _draw_game(self):
        self.data.refresh_matchup()
        matchup = self.data.matchup
        opp_av = matchup['opp_av']
        user_av = matchup['user_av']
        if opp_av is None:
            opp_av = 'noneLogo.png'
        if user_av is None:
            user_av = 'noneLogo.png'
        user_score = matchup.get('user_score')
        opp_score = matchup.get('opp_score')
        self.data.needs_refresh = True
        extra_sleep = 0
        while True:
            # Refresh the data
            if self.data.needs_refresh and self.data.check_scores:
                debug.log('Refresh game matchup')
                extra_sleep = 0
                self.data.refresh_scores()
                self.data.needs_refresh = False
            else:
                debug.info('Not refreshing, will update in 1 minute')
                extra_sleep = 40
                self.data.check_if_playing()
                self.data.needs_refresh = True
            if self.data.matchup:
                # colours
                opp_colour = (255, 255, 255)
                user_colour = (255, 255, 255)
                matchup = self.data.matchup
                game_date = 'WEEK {}'.format(self.data.week)
                # --- Other league games: flash up on a change, otherwise every LEAGUE_EVERY ---
                games = matchup.get('league') or []
                self._note_league_changes(games)
                mine_changed = (matchup['user_score'] != user_score
                                or matchup['opp_score'] != opp_score)
                # A change in our own game always gets the screen first, and
                # at most one of these shows between looks at our matchup
                interrupted = False
                if not mine_changed:
                    if games and self._league_changed:
                        interrupted = self._draw_league(games, LEAGUE_FLASH)
                    elif games and t.time() - self._league_shown_at >= LEAGUE_EVERY:
                        interrupted = self._draw_league(games, LEAGUE_SHOW)
                    elif t.time() - self._live_games_shown_at >= LIVE_GAMES_EVERY:
                        self._live_games_shown_at = t.time()
                        live_games = self.data.live_games()
                        if live_games:
                            interrupted = self._draw_live_games(live_games, LIVE_GAME_SHOW)
                    elif t.time() - self._mlb_shown_at >= MLB_EVERY:
                        self._mlb_shown_at = t.time()
                        mlb_game = self.data.mlb_game(MLB_TEAM)
                        if mlb_game:
                            interrupted = self._draw_mlb_game(mlb_game, MLB_SHOW)
                    elif t.time() - self._next_game_shown_at >= NEXT_GAME_EVERY:
                        self._next_game_shown_at = t.time()
                        next_game = self.data.next_solo_game()
                        if next_game:
                            interrupted = self._draw_next_game(next_game, NEXT_GAME_SHOW)
                if interrupted:
                    # Our score moved behind another screen: straight back to the
                    # matchup, which shows the change
                    self.data.needs_refresh = True
                    continue
                # --- Team names (live view), each above its own logo ---
                _opp_name = self._display_name(matchup, 'opp')
                _user_name = self._display_name(matchup, 'user')
                debug.log("[live] display user='%s' opp='%s'" % (_user_name, _opp_name))
                self._place_text(_opp_name, self.font_mini, 0, 0, 30, centre_x=9)
                self._place_text(_user_name, self.font_mini, 34, 0, 30, align='right', centre_x=54)
                # --- end team names ---
                # small increase in score
                if matchup['user_score'] > user_score:
                    user_colour = (165, 200, 50)
                if matchup['opp_score'] > opp_score:
                    opp_colour = (165, 200, 50)
                # decrease in score
                if matchup['user_score'] < user_score:
                    user_colour = (175, 25, 25)
                if matchup['opp_score'] < opp_score:
                    opp_colour = (175, 25, 25)
                # big play! 5+ points for someone, turn it gold
                if matchup.get('user_score', 0) > (user_score + 5) or matchup.get('opp_score', 0) > (opp_score + 5):
                    debug.info("BIG PLAY ANIMATION DRAWN")
                    self._draw_big_play()
                if matchup['user_score'] > user_score + 5:
                    user_colour = (255, 215, 0)
                if matchup['opp_score'] > opp_score + 5:
                    opp_colour = (255, 215, 0)
                # Layout: names rows 1-5, logos rows 6-24, scores rows 26-30;
                # WEEK and score changes sit in the column between the logos (x=20..44)
                # Whole points in bold, then '.xx' in the thinner font
                opp_big, opp_small = '{:.2f}'.format(matchup['opp_score']).split('.')
                user_big, user_small = '{:.2f}'.format(matchup['user_score']).split('.')
                opp_small, user_small = '.' + opp_small, '.' + user_small
                opp_big_w = self.font_score.getbbox(opp_big)[2]
                opp_small_w = self.font_mini.getbbox(opp_small)[2]
                user_big_w = self.font_score.getbbox(user_big)[2]
                user_small_w = self.font_mini.getbbox(user_small)[2]
                score_y = 25
                self.draw.text((0, score_y), opp_big, fill=opp_colour, font=self.font_score)
                self.draw.text((opp_big_w, score_y), opp_small, fill=opp_colour, font=self.font_mini)
                user_small_x = self.width - user_small_w
                user_big_x = user_small_x - user_big_w
                self.draw.text((user_big_x, score_y), user_big, fill=user_colour, font=self.font_score)
                self.draw.text((user_small_x, score_y), user_small, fill=user_colour, font=self.font_mini)
                # Score changes: opponent's upper left, user's lower right of the middle column
                if abs(opp_score - matchup['opp_score']) > 0:
                    opp_diff = '{:0.2f}'.format(abs(opp_score - matchup['opp_score']))
                    self.draw.text((20, 12), opp_diff, fill=opp_colour, font=self.font_mini)
                if abs(user_score - matchup['user_score']) > 0:
                    user_diff = '{:0.2f}'.format(abs(user_score - matchup['user_score']))
                    self.draw.text((45 - self.font_mini.getbbox(user_diff)[2], 18),
                                   user_diff, fill=user_colour, font=self.font_mini)
                _bbox = self.font_mini.getbbox(game_date)
                _width = _bbox[2] - _bbox[0]
                game_date_pos = center_text(_width, 32)
                self.draw.text((game_date_pos, 6), game_date, fill=(255, 255, 255), font=self.font_mini)
                # --- Projected points difference (user minus opponent), between the scores ---
                try:
                    user_proj = float(matchup.get('user_proj', 0) or 0)
                    opp_proj = float(matchup.get('opp_proj', 0) or 0)
                    proj_diff = user_proj - opp_proj
                    prefix = '+' if proj_diff >= 0 else ''
                    diff_text = f"{prefix}{proj_diff:.1f}"

                    # Color thresholds: green if > 5, orange if between 5 and 0 (inclusive), red if < 0
                    if proj_diff > 5:
                        diff_color = (0, 128, 0)     # green
                    elif proj_diff >= 0:
                        diff_color = (255, 165, 0)     # orange
                    else:
                        diff_color = (255, 44, 44)     # red

                    diff_bbox = self.font_mini.getbbox(diff_text)
                    diff_width = diff_bbox[2] - diff_bbox[0]
                    diff_pos = int(center_text(diff_width, 32))
                    # Skip it rather than overlap a wide score
                    if diff_pos >= opp_big_w + opp_small_w + 2 and diff_pos + diff_width <= user_big_x - 2:
                        self.draw.text((diff_pos, score_y), diff_text, fill=diff_color, font=self.font_mini)
                except Exception:
                    # If parsing fails or keys are missing, silently skip rendering the diff
                    pass
                if self.data.platform == "yahoo":
                    # Open the logo image file
                    opp_logo = Image.open(
                        'logos/{}.jpg'.format(opp_av)).resize((19, 19), Image.BOX)
                    user_logo = Image.open(
                        'logos/{}.jpg'.format(user_av)).resize((19, 19), Image.BOX)
                elif self.data.platform == "espn":
                    opp_logo = Image.open(
                        'logos/{}'.format(opp_av)).resize((19, 19), Image.BOX)
                    user_logo = Image.open(
                        'logos/{}'.format(user_av)).resize((19, 19), Image.BOX)
                else:
                    # try png for sleeper/espn (hopefully)
                    opp_logo = Image.open(
                        'logos/{}.png'.format(opp_av)).resize((19, 19), Image.BOX)
                    user_logo = Image.open(
                        'logos/{}.png'.format(user_av)).resize((19, 19), Image.BOX)
                # Composite the logos into the frame so _hold() can redraw it
                self.image.paste(opp_logo.convert("RGB"), (0, 6))
                self.image.paste(user_logo.convert("RGB"), (45, 6))
                if not self._matchup_live():
                    # Neither side has anyone on yet (or any more): keep to the
                    # other screens, and between them say when our next game is
                    self._scrollers = []
                    self._paint_next_kickoff(self._next_kickoff)
                self._frame = self.image
                self._show(self._frame)
                self._check_week_over()
                if self._games_over():
                    # Hand back to __render_game, which dims into standby
                    debug.info('No game with our starters on, leaving live view')
                    self._idle_since = None
                    self._scrollers = []
                    self.image = Image.new('RGB', (self.width, self.height))
                    self.draw = ImageDraw.Draw(self.image)
                    return
                # Refresh the Data image.
                self.image = Image.new('RGB', (self.width, self.height))
                self.draw = ImageDraw.Draw(self.image)
                # Save the scores.
                opp_score = matchup['opp_score']
                user_score = matchup['user_score']
                self._shown_scores = (user_score, opp_score)
                self.data.needs_refresh = True
                self._hold(10 + extra_sleep)
            else:
                # this doesn't work lul need 2 fix
                # (Need to make the screen run on it's own) If connection to the API fails, show bottom red line and refresh in 30s.
                self.draw.line((0, self.height) +
                               (self.width, self.height), fill=128)
                self.canvas = self.matrix.SwapOnVSync(self.canvas)
                t.sleep(30)

    # I think this is fine?
    def _draw_post_game(self):
        self.data.refresh_matchup()
        if self.data.matchup != 0:
            matchup = self.data.matchup
            opp_av = matchup['opp_av']
            user_av = matchup['user_av']
            if opp_av is None:
                opp_av = 'noneLogo.png'
            if user_av is None:
                user_av = 'noneLogo.png'
            # testing
            # Using big and small numbers
            # this is so so so terrible, I know but idc come at me I'll fix it eventually when I'm not tired and trying random chit
            opp_big, opp_small = divmod(matchup['opp_score'], 1)
            opp_big = int(opp_big)
            opp_small = int(round(opp_small, 2) * 100)
            if opp_small < 10:
                opp_small = '0' + str(opp_small)
                opp_small_score = '{}'.format(opp_small)
            else:
                opp_small_score = '{0:02d}'.format(opp_small)
            user_big, user_small = divmod(matchup['user_score'], 1)
            user_big = int(user_big)
            user_small = int(round(user_small, 2) * 100)
            if user_small < 10:
                user_small = '0' + str(user_small)
                user_small_score = '{}'.format(user_small)
            else:
                user_small_score = '{0:02d}'.format(user_small)
            opp_big_size = self.font.getbbox(str(opp_big))[0]
            opp_small_size = self.font_mini.getbbox(str(opp_small))[0]
            user_big_size = self.font.getbbox(str(user_big))[0]
            user_small_size = self.font_mini.getbbox(str(user_small))[0]
            user_big_score = '{}'.format(user_big)
            opp_big_score = '{}'.format(opp_big)
            # trying to centre them to make it a bit more a e s t h e t i c (essentially adding padding)
            left_offset = int(math.floor(opp_big / 100))
            # end testing
            # Prepare the data
            game_date = 'WEEK {}'.format(self.data.week)
            result = ''
            opp_colour = (255, 255, 255)
            user_colour = (255, 255, 255)
            if matchup['opp_score'] > matchup['user_score']:
                result = 'LOSS'
                opp_colour = (25, 200, 25)
                user_colour = (200, 25, 25)
            else:
                result = 'WIN'
                opp_colour = (200, 25, 25)
                user_colour = (25, 200, 25)
            self.draw.multiline_text(
                (left_offset, 19), opp_big_score, fill=opp_colour, font=self.font, align="left")
            self.draw.multiline_text((opp_big_size + left_offset, 19), opp_small_score,
                                     fill=opp_colour, font=self.font_mini, align="left")
            self.draw.multiline_text((self.width - user_small_size - user_big_size, 19),
                                     user_big_score, fill=user_colour, font=self.font, align="right")
            self.draw.multiline_text((self.width - user_small_size, 19), user_small_score,
                                     fill=user_colour, font=self.font_mini, align="right")
            # Set the position of the information on screen.
            _bbox = self.font_mini.getbbox(game_date)
            _width = _bbox[2] - _bbox[0]
            game_date_pos = center_text(_width, 32)
            res_bbox = self.font_res.getbbox(result)
            result_pos = center_text(res_bbox[2] - res_bbox[0], 32)
            self.draw.text(
                (game_date_pos, 7),
                game_date,
                fill=(255, 255, 255),
                font=self.font_mini,
                align="center"
            )
            self.draw.multiline_text((result_pos, 9), result, fill=(
                255, 255, 255), font=self.font_res, align="center")
            # Open the logo image file
            if self.data.platform == "yahoo":
                # Open the logo image file
                opp_logo = Image.open(
                    'logos/{}.jpg'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}.jpg'.format(user_av)).resize((19, 19), Image.BOX)
            elif self.data.platform == "espn":
                opp_logo = Image.open(
                    'logos/{}'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}'.format(user_av)).resize((19, 19), Image.BOX)
            else:
                # try png for sleeper/espn (hopefully)
                opp_logo = Image.open(
                    'logos/{}.png'.format(opp_av)).resize((19, 19), Image.BOX)
                user_logo = Image.open(
                    'logos/{}.png'.format(user_av)).resize((19, 19), Image.BOX)
            # Set the position of each logo on screen.
            opp_team_logo_pos = {"x": 0, "y": 0}
            user_team_logo_pos = {"x": 45, "y": 0}
            # Put the data on the canvas
            self.canvas.SetImage(self.image, 0, 0)
            # Put the images on the canvas
            self.canvas.SetImage(opp_logo.convert(
                "RGB"), opp_team_logo_pos["x"], opp_team_logo_pos["y"])
            self.canvas.SetImage(user_logo.convert(
                "RGB"), user_team_logo_pos["x"], user_team_logo_pos["y"])
            # Load the canvas on screen.
            self.canvas = self.matrix.SwapOnVSync(self.canvas)
            # Refresh the Data image.
            self.image = Image.new('RGB', (self.width, self.height))
            self.draw = ImageDraw.Draw(self.image)
        else:
            # (Need to make the screen runs) If connection to the API fails, show bottom red line and refresh in 1 min.
            self.draw.line((0, 0) + (self.width, 0), fill=128)
            self.canvas = self.matrix.SwapOnVSync(self.canvas)
            t.sleep(60)  # sleep for 1 min

    def _draw_big_play(self):
        debug.info('big play woo!')
        # Load the gif file
        im = Image.open("Assets/big_play_animation.gif")
        # Set the frame index to 0
        frameNo = 0
        # Go through the frames
        play = True
        # Overlay the GIF frames over the existing canvas without clearing
        while play:
            try:
                im.seek(frameNo)
            except EOFError:
                play = False
                break
            frame = im.convert('RGBA')
            bg_image = Image.new('RGBA', (self.width, self.height))
            bg_image.paste(frame, (0, 0), frame)  # overlay alpha channel
            self.canvas.SetImage(bg_image.convert('RGB'), 0, 0)
            self.canvas = self.matrix.SwapOnVSync(self.canvas)
            frameNo += 1
            t.sleep(0.05)

    def _draw_off_season(self):
        # Refresh canvas
        # self.image = Image.new('RGB', (self.width, self.height))
        # self.draw = ImageDraw.Draw(self.image)
        off_pos = center_text(self.font.getbbox("OFF")[0], 32)
        szn_pos = center_text(self.font.getbbox("SEASON")[0], 32)
        self.draw.multiline_text((off_pos, 3), "OFF", fill=(
            255, 255, 255), font=self.font, align="center")
        self.draw.multiline_text((szn_pos, self.font.getbbox("SEASON")[
                                 1]+4), "SEASON", fill=(255, 255, 255), font=self.font, align="center")
        self.canvas.SetImage(self.image, 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)

    def _draw_days_until_kickoff(self):
        off_pos = center_text(self.font.getbbox('KICKOFF IN')[0], 32)
        szn_pos = center_text(self.font.getbbox(self.data.start_dt)[0], 32)
        self.draw.multiline_text((off_pos, 3), 'KICKOFF IN', fill=(
            255, 255, 255), font=self.font, align="center")
        self.draw.multiline_text((szn_pos, self.font.getbbox(self.data.start_dt)[
                                 1]+4), self.data.start_dt, fill=(255, 255, 255), font=self.font, align="center")
        # self._refresh_image()
