from datetime import datetime, timezone
import math
import os
import data.sleeper_api_parser as sleeper
import data.yahoo_api_parser as yahoo
import data.espn_api_parser as espn
import debug
import requests

API_URL = 'http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
MLB_URL = 'http://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard'
# ESPN team logos, the dark-background version first; cached under logos/
LOGO_URLS = ('https://a.espncdn.com/i/teamlogos/{0}/500-dark/{1}.png',
             'https://a.espncdn.com/i/teamlogos/{0}/500/{1}.png')


class Data:
    def __init__(self, config):
        # Save the parsed config
        self.config = config

        # Get what week it is
        self.week = self.get_week()
        self.season_type = self.get_season_type()

        # which platform are we using
        self.platform = self.config.platform
        self.api = self.choose_api()

        # Flag to determine when to refresh data
        self.needs_refresh = True
        self.check_scores = True

        self.matchup = self.api.matchup

    def choose_api(self):
        debug.info(self.platform.lower())
        if self.platform.lower() == "sleeper":
            return sleeper.SleeperFantasyInfo(self.config.sleeper_league_id, self.config.sleeper_user_id, self.week)
        elif self.platform.lower() == "yahoo":
            return yahoo.YahooFantasyInfo(self.config.yahoo_consumer_key, self.config.yahoo_consumer_secret, self.config.yahoo_game_id, self.config.yahoo_league_id, self.config.yahoo_team_id, self.week)
        elif self.platform.lower() == "espn":
            return espn.ESPNFantasyInfo(self.config.espn_league_id, self.config.espn_team_id, self.config.espn_swid, self.config.espn_s2, self.week, self.config.season)
        else:
            # this will break but I'll robustify it later
            print('You need to set one of ESPN, Yahoo, or Sleeper in the config file')
            return 0

    def get_season_type(self):
        # this is for that gap on espn where it's after the end of preseason, but there are like 12 days before the season starts
        season_type = requests.get(API_URL).json()
        if season_type['season']['type'] == 2 and season_type['leagues'][0]['season']['type']['type'] != 2:
            return 'kickoff'
        else:
            return 'season'

    def get_week(self):
        week_info = requests.get(API_URL).json()
        return week_info['week']['number']

    def refresh_week(self):
        self.week = self.get_week()
        self.api.week = self.week

    def _scoreboard_events(self, week=None):
        url = API_URL if week is None else '{0}?week={1}'.format(API_URL, week)
        return requests.get(url).json().get('events', [])

    def _relevant_teams(self):
        # NFL teams our matchup has starters on, or None to count every game
        # (other platforms, or a failed lookup - better awake than asleep)
        lookup = getattr(self.api, 'starting_nfl_teams', None)
        if lookup is None:
            return None
        try:
            return lookup()
        except Exception as error:
            debug.warning('could not look up starters: {0}'.format(error))
            return None

    @staticmethod
    def _involving(events, teams):
        if teams is None:
            return events
        return [e for e in events
                if {c['team']['abbreviation'] for c in e['competitions'][0]['competitors']} & teams]

    @staticmethod
    def _event_state(event):
        return event['status']['type']['state']

    @staticmethod
    def _earliest_kickoff(events):
        times = [datetime.fromisoformat(e['date'].replace('Z', '+00:00'))
                 for e in events if Data._event_state(e) == 'pre']
        return min(times) if times else None

    def next_kickoff(self):
        # (0, None) while a game is in progress. None means the schedule is
        # unknown, and the caller should stay awake rather than risk sleeping
        # through a game. Only games with one of our matchup's starters count.
        try:
            events = self._involving(self._scoreboard_events(), self._relevant_teams())
            if any(self._event_state(e) == 'in' for e in events):
                return (0, None)
            kickoff = self._earliest_kickoff(events)
            if kickoff is None:
                kickoff = self._earliest_kickoff(
                    self._scoreboard_events(self.week + 1))
            if kickoff is None:
                return None
        except Exception as error:
            debug.warning(
                'could not determine next kickoff: {0}'.format(error))
            return None
        delta = (kickoff - datetime.now(timezone.utc)).total_seconds()
        return (max(0, delta), kickoff)

    def next_solo_game(self):
        # The next NFL game to kick off, if it has its slot to itself (Thursday
        # or Monday night, an early international game), with its betting line.
        # None when the next slot has several games or ESPN can't be reached.
        try:
            events = [e for e in self._scoreboard_events() if self._event_state(e) == 'pre']
            if not events:
                events = [e for e in self._scoreboard_events(self.week + 1)
                          if self._event_state(e) == 'pre']
        except Exception as error:
            debug.warning('could not look up the next game: {0}'.format(error))
            return None
        kickoff = self._earliest_kickoff(events)
        slot = [e for e in events
                if datetime.fromisoformat(e['date'].replace('Z', '+00:00')) == kickoff]
        if len(slot) != 1:
            return None
        competition = slot[0]['competitions'][0]
        teams = {c['homeAway']: c['team']['abbreviation'] for c in competition['competitors']}
        odds = (competition.get('odds') or [{}])[0]
        starters = self._starters_in(teams.values())
        return {
            'league': 'nfl',
            'away': teams.get('away', '?'),
            'home': teams.get('home', '?'),
            'neutral': competition.get('neutralSite', False),
            'kickoff': kickoff,
            'spread': odds.get('details'),  # e.g. "GB -4.5", or "EVEN"
            'over_under': odds.get('overUnder'),
            'user_starters': starters['user'],
            'opp_starters': starters['opp'],
        }

    def _starters_in(self, nfl_teams):
        # How many of each side's starters play for these NFL teams, as
        # {'user': n, 'opp': n}; None each when unknown (Yahoo only)
        starters = {'user': None, 'opp': None}
        lookup = getattr(self.api, 'starter_counts', None)
        if lookup is not None:
            try:
                counts = lookup()
                for side in starters:
                    starters[side] = sum(counts[side].get(team, 0) for team in nfl_teams)
            except Exception as error:
                debug.warning('could not look up starters: {0}'.format(error))
        return starters

    def live_games(self):
        # NFL games in progress with a starter from either side of our matchup
        try:
            events = self._involving(self._scoreboard_events(), self._relevant_teams())
        except Exception as error:
            debug.warning('could not look up live games: {0}'.format(error))
            return []
        games = []
        for event in events:
            if self._event_state(event) != 'in':
                continue
            competition = event['competitions'][0]
            sides = {c['homeAway']: c for c in competition['competitors']}
            has_ball = (competition.get('situation') or {}).get('possession')
            starters = self._starters_in([c['team']['abbreviation'] for c in sides.values()])
            games.append({
                'league': 'nfl',
                'away': sides['away']['team']['abbreviation'],
                'home': sides['home']['team']['abbreviation'],
                'away_score': int(sides['away'].get('score') or 0),
                'home_score': int(sides['home'].get('score') or 0),
                'clock': event['status']['type']['shortDetail'],  # e.g. "15:00 - 2nd", "Halftime"
                'possession': next((side for side, c in sides.items() if c['team']['id'] == has_ball), None),
                'user_starters': starters['user'],
                'opp_starters': starters['opp'],
            })
        return games

    def mlb_game(self, team):
        # `team`'s MLB game while it's in progress, None otherwise or if ESPN
        # can't be reached
        try:
            events = requests.get(MLB_URL, timeout=10).json().get('events', [])
        except Exception as error:
            debug.warning('could not look up MLB scores: {0}'.format(error))
            return None
        for event in events:
            competition = event['competitions'][0]
            sides = {c['homeAway']: c for c in competition['competitors']}
            if (self._event_state(event) != 'in'
                    or team not in {c['team']['abbreviation'] for c in sides.values()}):
                continue
            situation = competition.get('situation') or {}
            return {
                'league': 'mlb',
                'away': sides['away']['team']['abbreviation'],
                'home': sides['home']['team']['abbreviation'],
                'away_score': int(sides['away'].get('score') or 0),
                'home_score': int(sides['home'].get('score') or 0),
                'inning': event['status']['type']['shortDetail'],  # e.g. "Bot 7th"
                'outs': situation.get('outs'),
                'bases': [situation.get(b, False) for b in ('onFirst', 'onSecond', 'onThird')],
            }
        return None

    @staticmethod
    def team_logo(league, abbr):
        # Path to an ESPN team logo, downloaded the first time it's asked for;
        # None if ESPN doesn't have one
        path = 'logos/espn_{0}_{1}.png'.format(league, abbr.lower())
        if os.path.exists(path):
            return path
        for url in LOGO_URLS:
            try:
                response = requests.get(url.format(league, abbr.lower()), timeout=10)
            except Exception as error:
                debug.warning('could not fetch {0} logo: {1}'.format(abbr, error))
                return None
            if response.ok:
                with open(path, 'wb') as logo:
                    logo.write(response.content)
                return path
        return None

    def week_finished(self):
        # True once every game with one of our matchup's starters is final this
        # week, None if ESPN can't be reached
        try:
            events = self._involving(self._scoreboard_events(), self._relevant_teams())
        except Exception as error:
            debug.warning('could not check for end of week: {0}'.format(error))
            return None
        return bool(events) and all(self._event_state(e) == 'post' for e in events)

    def get_current_date(self):
        # pretty dumb function but whatever
        return datetime.now(timezone.utc)

    def refresh_matchup(self):
        self.matchup = self.api.refresh_matchup()
        self.needs_refresh = False

    # this looks rough
    def refresh_scores(self):
        self.matchup = self.api.refresh_scores()
        self.needs_refresh = False

    def refresh_rosters(self):
        self.teams_info = self.api.get_teams(self.config.league_id)

    def get_players(self):
        user = next(
            (item for item in self.teams_info if item['id'] == self.user_id))
        return user['players']

    def test_game(self, n):
        return self.api.get_test_scores(n)

    # def refresh_draft(self):
    #     self.draft = sleeper.get_draft(self.league_id)
    #     self.draft_status = self.draft['status']
    #     self.draft_start = self.draft['start_time']
    #     self.draft_sleep = 43200
    #     if self.draft_start:
    #         draft_delta = datetime.fromtimestamp(self.draft_start/1000.0) - datetime.now()
    #         self.draft_dt = self.set_dt(draft_delta)
    #     else:
    #         self.draft_dt = 'NOT SET'
    #     self.draft_needs_refresh = False

    def refresh_start(self):
        self.sleep = 43200
        start_delta = datetime.strptime("{} 20:20:00 EDT".format(
            self.config.opening_day), "%Y-%m-%d %H:%M:%S %Z") - datetime.now()
        self.start_dt = self.set_dt(start_delta)

    def set_dt(self, old_dt):
        if old_dt.days == 1:
            new_dt = '{} DAY'.format(old_dt.days)
        elif old_dt.days > 0:
            new_dt = '{} DAYS'.format(old_dt.days)
        elif (old_dt.seconds / 3600) > 0:
            new_dt = '{} HOURS'.format(old_dt.seconds / 3600)
            self.sleep = 3600
        elif (old_dt.seconds / 60) > 0:
            if (old_dt.seconds / 60) == 1:
                new_dt = '{} MINUTE'.format(old_dt.seconds / 60)
            else:
                new_dt = '{} MINUTES'.format(old_dt.seconds / 60)
            self.sleep = 60
        else:
            new_dt = '{} SECONDS'.format(old_dt.seconds)
            self.sleep = 0.1  # turbo mode let's go
        return new_dt

    def check_if_playing(self):
        time = self.get_current_date()
        # thursday, sunday, monday
        # I gotta find a better way to do this but I ain't doin' it now
        if (time.weekday() == 4 and time.hour >= 0 and time.hour <= 4) or ((time.weekday() == 6 and time.hour >= 13) or (time.weekday() == 0 and time.hour <= 4)) or ((time.weekday() == 0 and time.hour >= 19) or (time.weekday() == 1 and time.hour <= 4)):
            self.check_scores = True
        else:
            self.check_scores = False
