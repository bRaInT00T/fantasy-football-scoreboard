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
        # Create a new data image.
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)
        # Load the fonts
        self.font = ImageFont.truetype("fonts/score_large.otf", 16)
        self.font_mini = ImageFont.truetype("fonts/04B_24__.TTF", 8)
        self.font_vs = ImageFont.truetype("fonts/CG pixel 3x5.ttf", 10)
        self.font_res = ImageFont.truetype("fonts/CG pixel 3x5.ttf", 6)
        # Names too wide for their box, animated by _hold()
        self._scrollers = []
        self._scroll_tick = 0
        self._frame = None

    @staticmethod
    def _display_name(matchup, side):
        # Team name first; the owner only when the platform gave no team name
        return (matchup.get(side + '_team') or matchup.get(side + '_name') or '').strip()

    def _place_text(self, text, font, x, y, width, align='left'):
        """Draw text in a box of the given width, or queue it to scroll if it won't fit."""
        text_w = font.getbbox(text)[2] if text else 0
        if text_w <= width:
            dx = width - text_w if align == 'right' else 0
            self.draw.text((x + dx, y), text, fill=(255, 255, 255), font=font)
            return
        strip = Image.new('RGB', (text_w + SCROLL_GAP, font.getbbox(text)[3]))
        ImageDraw.Draw(strip).text((0, 0), text, fill=(255, 255, 255), font=font)
        self._scrollers.append((strip, x, y, width))

    def _show(self, image):
        self.canvas.SetImage(image, 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def _hold(self, seconds):
        """Keep the last frame up for `seconds`, scrolling any names that didn't fit."""
        if not self._scrollers or self._frame is None:
            self._scrollers = []
            t.sleep(seconds)
            return
        end = t.time() + seconds
        while t.time() < end:
            frame = self._frame.copy()
            for strip, x, y, width in self._scrollers:
                offset = self._scroll_tick % strip.width
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

    def _draw_standby(self, kickoff):
        local = kickoff.astimezone()
        label = 'NEXT: {0} {1}:{2:02d}{3}'.format(
            local.strftime('%a').upper(),
            local.hour % 12 or 12,
            local.minute,
            'AM' if local.hour < 12 else 'PM')
        self.image = Image.new('RGB', (self.width, self.height))
        self.draw = ImageDraw.Draw(self.image)
        pos = center_text(self.font_mini.getbbox(label)[2], 32)
        self.draw.multiline_text((pos, 12), label, fill=(
            110, 110, 110), font=self.font_mini, align="center")
        self.canvas.SetImage(self.image, 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def _standby(self):
        config = self.data.config
        if not config.sleep_enabled:
            self._in_standby = False
            return False
        upcoming = self.data.next_kickoff()
        # Unknown schedule: stay awake rather than risk sleeping through a game
        if upcoming is None:
            self._in_standby = False
            return False
        seconds, kickoff = upcoming
        # Takes a long gap to drop into standby, but once there it stays until
        # just before kickoff - otherwise it would wake a full sleep_after
        # ahead of the game and wake_before_hours would never apply
        threshold = (config.wake_before_hours if self._in_standby
                     else config.sleep_after_hours)
        if seconds < threshold * 3600:
            self._in_standby = False
            return False
        nap = min(seconds - config.wake_before_hours * 3600, STANDBY_MAX_NAP)
        if nap <= 0:
            self._in_standby = False
            return False
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
            self._place_text(opp_name, self.font_mini, 0, 1, 30)
            self._place_text(user_name, self.font_mini, 34, 1, 30, align='right')
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
                # --- Team names (live view) ---
                # Both share the gap between the logos (x=20..44) on the top row
                _opp_name = self._display_name(matchup, 'opp')
                _user_name = self._display_name(matchup, 'user')
                debug.log("[live] display user='%s' opp='%s'" % (_user_name, _opp_name))
                name_x, name_y, name_w = 20, 1, 25
                opp_w = self.font_mini.getbbox(_opp_name)[2] if _opp_name else 0
                user_w = self.font_mini.getbbox(_user_name)[2] if _user_name else 0
                if opp_w + user_w + 3 <= name_w:
                    self._place_text(_opp_name, self.font_mini, name_x, name_y, name_w)
                    self._place_text(_user_name, self.font_mini, name_x, name_y, name_w, align='right')
                else:
                    self._place_text('{0} VS {1}'.format(_opp_name, _user_name),
                                     self.font_mini, name_x, name_y, name_w)
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
                # Using big and small numbers
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
                opp_diff = '{:0.2f}'.format(
                    abs(opp_score - matchup['opp_score']))
                user_diff = '{:0.2f}'.format(
                    abs(user_score - matchup['user_score']))
                opp_big_size = self.font.getbbox(str(opp_big))[2]
                opp_small_size = self.font_mini.getbbox(str(opp_small))[2]
                user_big_size = self.font.getbbox(str(user_big))[2]
                user_small_size = self.font_mini.getbbox(str(user_small))[2]
                user_diff_size = self.font_mini.getbbox(user_diff)[0]
                opp_diff_size = self.font_mini.getbbox(opp_diff)[0]
                # this is bad form I know but idc come at me I'll fix it eventually when I'm not tired and trying random chit
                opp_big_score = '{}'.format(opp_big)
                user_big_score = '{}'.format(user_big)
                # trying to centre them to make it a bit more a e s t h e t i c (essentially adding padding)
                # ((self.width / 2) - (opp_big_size + opp_small_size)) / 2 - 2
                left_offset = int(math.floor(opp_big / 100))
                # eventually may colour differently depending on score advantage
                self.draw.multiline_text(
                    (left_offset, 19), opp_big_score, fill=opp_colour, font=self.font, align="left")
                self.draw.multiline_text((opp_big_size + left_offset, 19), opp_small_score,
                                         fill=opp_colour, font=self.font_mini, align="left")
                user_big_width = self.font.getbbox(str(user_big))[2]
                user_small_width = self.font_mini.getbbox(str(user_small))[2]

                self.draw.multiline_text(
                    (self.width - user_small_width - user_big_width, 19),
                    user_big_score, fill=user_colour, font=self.font, align="left"
                )
                self.draw.multiline_text(
                    (self.width - user_small_width, 19),
                    user_small_score, fill=user_colour, font=self.font_mini, align="left"
                )
                # diffs
                if abs(opp_score - matchup['opp_score']) > 0:
                    self.draw.multiline_text(
                        (21, 6), opp_diff, fill=opp_colour, font=self.font_mini, align="left")
                if abs(user_score - matchup['user_score']) > 0:
                    self.draw.multiline_text((self.width - 20 - user_diff_size, 12),
                                             user_diff, fill=user_colour, font=self.font_mini, align="right")
                # Set the projections on the screen?
                _bbox = self.font_mini.getbbox(game_date)
                _width = _bbox[2] - _bbox[0]
                game_date_pos = center_text(_width, 32)
                self.draw.text(
                    (game_date_pos, 7),  # push baseline further down to guarantee top row visible
                    game_date,
                    fill=(255, 255, 255),
                    font=self.font_mini,
                    align="center"
                )
                # --- Projected points difference (user minus opponent), centered under week label ---
                try:
                    user_proj = float(matchup.get('user_proj', 0) or 0)
                    opp_proj = float(matchup.get('opp_proj', 0) or 0)
                    proj_diff = user_proj - opp_proj
                    prefix = '+' if proj_diff >= 0 else ''
                    diff_text = f" {prefix}{proj_diff:.1f}"

                    # Color thresholds: green if > 5, orange if between 5 and 0 (inclusive), red if < 0
                    if proj_diff > 5:
                        diff_color = (0, 128, 0)     # green
                    elif proj_diff >= 0:
                        diff_color = (255, 165, 0)     # orange
                    else:
                        diff_color = (255, 44, 44)     # red

                    diff_bbox = self.font_mini.getbbox(diff_text)
                    diff_width = diff_bbox[2] - diff_bbox[0]
                    diff_pos = center_text(diff_width, 32)

                    self.draw.text(
                        (diff_pos, 14),
                        diff_text,
                        fill=diff_color,
                        font=self.font_mini,
                        align="center"
                    )
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
                self.image.paste(opp_logo.convert("RGB"), (0, 0))
                self.image.paste(user_logo.convert("RGB"), (45, 0))
                self._frame = self.image
                self._show(self._frame)
                # Refresh the Data image.
                self.image = Image.new('RGB', (self.width, self.height))
                self.draw = ImageDraw.Draw(self.image)
                # Save the scores.
                opp_score = matchup['opp_score']
                user_score = matchup['user_score']
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
