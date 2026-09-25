import requests
from datetime import datetime
import os
import debug
import json
import time
from yahoo_oauth import OAuth2

# Lineup slots that don't score, and Yahoo team codes that differ from ESPN's
NON_STARTING_SLOTS = {'BN', 'IR', 'IR+', 'NA'}
YAHOO_TO_ESPN_TEAM = {'WAS': 'WSH'}
# Seconds to reuse the starters lookup; lineups rarely change mid-window
STARTERS_TTL = 900


class YahooAPIError(Exception):
    pass


def _describe_error(response, body):
    description = (body.get("error") or {}).get("description", "").strip()
    return "Yahoo API returned {0}: {1}".format(
        response.status_code, description or response.text[:200].strip())


class YahooFantasyInfo():
    def __init__(self, yahoo_consumer_key, yahoo_consumer_secret, game_id, league_id, team_id, week):
        self.team_id = team_id
        self.league_id = league_id
        self.game_id = game_id
        self.week = week
        self.matchup_team_keys = []
        self.user_team_key = None
        self._starters = None
        self._starters_at = 0
        self.auth_info = {"consumer_key": yahoo_consumer_key,
                          "consumer_secret": yahoo_consumer_secret}
        
        authpath = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', 'auth'))
        if not os.path.exists(authpath):
            os.makedirs(authpath, 0o777)

        # load or create OAuth2 refresh token
        token_file_path = os.path.join(authpath, "token.json")
        if os.path.isfile(token_file_path):
            with open(token_file_path) as yahoo_oauth_token:
                self.auth_info = json.load(yahoo_oauth_token)
        else:
            with open(token_file_path, "w") as yahoo_oauth_token:
                json.dump(self.auth_info, yahoo_oauth_token)

        if "access_token" in self.auth_info.keys():
            self._yahoo_access_token = self.auth_info["access_token"]

        # complete OAuth2 3-legged handshake by either refreshing existing token or requesting account access
        # and returning a verification code to input to the command line prompt
        self.oauth = OAuth2(None, None, from_file=token_file_path)

        # Auto-resolve game_id from Yahoo if not provided or set to a placeholder
        if not self.game_id or str(self.game_id).lower() == "auto":
            try:
                self.game_id = self.get_game_id_for_season()
            except Exception:
                # Fallback: Yahoo allows using game_code (e.g., "nfl") as game_key for current season
                self.game_id = "nfl"

        self.matchup = self.get_matchup(
            self.game_id, self.league_id, self.team_id, week)
        self.get_avatars(self.matchup)

    def get_league_teams_config(self, output_file="teams_config.json"):
        """Fetch all teams in the league and save their info to a config JSON file."""
        self.refresh_access_token()
        url = f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.game_id}.l.{self.league_id}/teams"
        resp = self.oauth.session.get(url, params={'format': 'json'})
        if resp.status_code != 200:
            raise RuntimeError(f"Yahoo /teams error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

        teams_config = {"teams": []}
        teams_data = data.get("fantasy_content", {}).get("games", {}).get("game", {}).get("0", {}).get("teams", {})
        # teams_data = data["fantasy_content"]["league"][1]["teams"]

        for t in teams_data.values():
            if isinstance(t, int):
                continue
            team = t["team"]
            # Extract info
            team_id = None
            team_name = None
            team_logo = ""
            manager_entry = {}

            for item in team:
                if isinstance(item, dict):
                    if "team_key" in item:
                        # last part after '.t.' is team_id
                        team_id = int(item["team_key"].split(".t.")[-1])
                    elif "name" in item:
                        team_name = item["name"]
                    elif "team_logos" in item:
                        team_logo = item["team_logos"][0]["team_logo"]["url"]
                    elif "managers" in item:
                        manager_entry = item["managers"][0]["manager"]

            teams_config["teams"].append({
                "team_id": team_id,
                "team_name": team_name,
                "manager": {
                    "first_name": manager_entry.get("first_name", ""),
                    "last_name": manager_entry.get("last_name", ""),
                    "nickname": manager_entry.get("nickname", "")
                },
                "team_logo": team_logo,
                "short_name": ""  # you can fill this manually later
            })

        # Write to file
        config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', output_file))
        with open(config_path, "w") as f:
            json.dump(teams_config, f, indent=2)
        print(f"✅ Saved team info to {config_path}")

        return teams_config

    def get_game_id_for_season(self, season: int = None):
        """
        Return the numeric Yahoo NFL game_key for the given season (defaults to current year).
        Uses /fantasy/v2/games;game_codes=nfl;seasons=YYYY and falls back to user.games.
        """
        self.refresh_access_token()
        if season is None:
            season = datetime.now().year
        # Primary: query the games collection for the specific season
        url = f"https://fantasysports.yahooapis.com/fantasy/v2/games;game_codes=nfl;seasons={season}"
        resp = self.oauth.session.get(url, params={'format': 'json'})
        if resp.status_code != 200:
            raise RuntimeError(f"Yahoo /games error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

        def extract_game_keys(games_node):
            keys = []
            for k, v in games_node.items():
                if k == "count":
                    continue
                # v is typically an object with key "game" -> list
                game_list = []
                try:
                    game_list = v["game"][0]
                except Exception:
                    game_list = v.get("game", [])
                info = {}
                for item in game_list:
                    if isinstance(item, dict):
                        info.update(item)
                gk = info.get("game_key")
                # Ensure the season matches what we asked for, if present
                if info.get("season") in (str(season), season) and gk:
                    try:
                        keys.append(int(gk))
                    except ValueError:
                        pass
            return keys

        games_node = data.get("fantasy_content", {}).get("games", {})
        keys = extract_game_keys(games_node)

        # Fallback: if the seasonal collection is empty, query the user's games and pick the latest NFL key
        if not keys:
            url2 = "https://fantasysports.yahooapis.com/fantasy/v2/users;use_login=1/games;game_codes=nfl"
            resp2 = self.oauth.session.get(url2, params={'format': 'json'})
            if resp2.status_code == 200:
                data2 = resp2.json()
                games_node2 = data2.get("fantasy_content", {}).get("users", {})
                # shape: users -> 0 -> user -> games -> 0 -> game -> [ {...} ]
                # walk defensively
                found = []
                try:
                    user_block = games_node2.get("0", {}).get("user", {})
                    user_games = user_block.get("games", {}).get("0", {}).get("game", [])
                    for g in user_games:
                        if isinstance(g, dict):
                            gk = g.get("game_key")
                            seas = g.get("season")
                            if gk and (seas in (str(season), season) or seas is None):
                                try:
                                    found.append(int(gk))
                                except ValueError:
                                    pass
                except Exception:
                    pass
                if found:
                    return str(max(found))

        if not keys:
            raise RuntimeError("Could not resolve NFL game_id from Yahoo /games endpoint")
        return str(max(keys))

    # yeah these two are stupid and useless functions but right now I'm panicking trying to get this to work
    def refresh_matchup(self):
        return self._refresh()

    def refresh_scores(self):
        return self._refresh()

    def _refresh(self):
        try:
            self.matchup = self.get_matchup(
                self.game_id, self.league_id, self.team_id, self.week)
        except YahooAPIError as error:
            # A refresh failure shouldn't take the board down mid-game
            debug.error("{0} - keeping last known scores".format(error))
        return self.matchup

    def get_matchup(self, game_id, league_id, team_id, week):
        self.refresh_access_token()
        # The league scoreboard has every matchup, so one call covers ours and the rest
        url = f"https://fantasysports.yahooapis.com/fantasy/v2/league/{self.game_id}.l.{self.league_id}/scoreboard;week={week}"
        response = self.oauth.session.get(url, params={'format': 'json'})
        try:
            data = response.json()
        except ValueError:
            data = {}

        if "fantasy_content" not in data:
            raise YahooAPIError(_describe_error(response, data))

        all_matchups = data["fantasy_content"]["league"][1]["scoreboard"]["0"]["matchups"]
        matchup = {}
        league = []
        for m in all_matchups:
            if isinstance(all_matchups[m], int):  # skip "count"
                continue
            teams = all_matchups[m]['matchup']['0']['teams']
            sides = []
            for t in ('0', '1'):
                info = {}
                for item in teams[t]['team'][0]:
                    if isinstance(item, dict):
                        info.update(item)
                sides.append({
                    'key': info.get('team_key'),
                    'name': info.get('name', 'Unknown'),
                    'score': float(teams[t]['team'][1].get('team_points', {}).get('total', 0) or 0),
                    'mine': info.get('is_owned_by_current_login') == 1
                            or str(info.get('team_id')) == str(self.team_id),
                })
            if any(side['mine'] for side in sides):
                matchup[m] = all_matchups[m]
                self.matchup_team_keys = [side['key'] for side in sides]
                self.user_team_key = next(side['key'] for side in sides if side['mine'])
            else:
                league.append(sides)
        matchup_info = {'league': league}

        for m in matchup:
            if not isinstance(matchup[m], int):  # skip "count"
                teams = matchup[m]['matchup']['0']['teams']
                for t in teams:
                    if not isinstance(teams[t], int):  # skip "count"
                        team_data = teams[t]['team'][0]  # this is the list of mixed dicts and empty lists

                        # helper to find dict by key in team_data list
                        def find_entry(key):
                            return next((item for item in team_data if isinstance(item, dict) and key in item), None)

                        manager_entry = find_entry('managers')
                        logo_entry = find_entry('team_logos')
                        name_entry = find_entry('name')

                        if manager_entry:
                            manager = manager_entry['managers'][0]['manager']
                            nickname = manager.get('nickname', 'Unknown')
                            image_url = manager.get('image_url', '')

                        else:
                            nickname = 'Unknown'
                            image_url = ''

                        logo_url = ''
                        if logo_entry:
                            logo_url = logo_entry['team_logos'][0]['team_logo']['url']

                        team_name = name_entry['name'] if name_entry else 'Unknown'

                        projected_points = teams[t]['team'][1].get('team_projected_points', {}).get('total', '0')
                        actual_points = teams[t]['team'][1].get('team_points', {}).get('total', '0')

                        # Determine if this is the user's team by checking "is_owned_by_current_login"
                        is_user_team = any(
                            isinstance(item, dict) and item.get('is_owned_by_current_login') == 1
                            for item in team_data
                        )

                        if is_user_team:
                            first = manager.get('first_name', '')
                            last = manager.get('last_name', '')
                            full_name = f"{first} {last}".strip()
                            if not full_name:
                                full_name = nickname
                            matchup_info['user_name'] = full_name
                            matchup_info['user_av'] = nickname
                            matchup_info['user_av_location'] = logo_url or image_url
                            matchup_info['user_team'] = team_name
                            matchup_info['user_proj'] = projected_points
                            matchup_info['user_score'] = float(actual_points)
                        else:
                            first = manager.get('first_name', '')
                            last = manager.get('last_name', '')
                            full_name = f"{first} {last}".strip()
                            if not full_name:
                                full_name = nickname
                            matchup_info['opp_name'] = full_name
                            matchup_info['opp_av'] = nickname
                            matchup_info['opp_av_location'] = logo_url or image_url
                            matchup_info['opp_team'] = team_name
                            matchup_info['opp_proj'] = projected_points
                            matchup_info['opp_score'] = float(actual_points)

        return matchup_info

    def starting_nfl_teams(self):
        """NFL teams (ESPN codes) with a starter from either side of our matchup."""
        counts = self.starter_counts()
        return set(counts['user']) | set(counts['opp'])

    def starter_counts(self):
        """Starters per NFL team (ESPN codes) for each side of our matchup,
        as {'user': {'GB': 2, ...}, 'opp': {...}}."""
        if self._starters is not None and time.time() - self._starters_at < STARTERS_TTL:
            return self._starters
        counts = {'user': {}, 'opp': {}}
        for key in self.matchup_team_keys:
            side = counts['user' if key == self.user_team_key else 'opp']
            self.refresh_access_token()
            url = f"https://fantasysports.yahooapis.com/fantasy/v2/team/{key}/roster;week={self.week}"
            response = self.oauth.session.get(url, params={'format': 'json'})
            try:
                data = response.json()
            except ValueError:
                data = {}
            if "fantasy_content" not in data:
                raise YahooAPIError(_describe_error(response, data))
            players = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
            for p in players:
                if isinstance(players[p], int):  # skip "count"
                    continue
                info = {}
                for item in players[p]['player'][0]:
                    if isinstance(item, dict):
                        info.update(item)
                slot = next((item['position'] for item in players[p]['player'][1]['selected_position']
                             if isinstance(item, dict) and 'position' in item), 'BN')
                if slot in NON_STARTING_SLOTS:
                    continue
                team = (info.get('editorial_team_abbr') or '').upper()
                team = YAHOO_TO_ESPN_TEAM.get(team, team)
                side[team] = side.get(team, 0) + 1
        self._starters, self._starters_at = counts, time.time()
        return counts

    def get_avatars(self, teams):
        self.refresh_access_token()
        debug.info('getting avatars')
        logospath = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', 'logos'))
        if not os.path.exists(logospath):
            os.makedirs(logospath, 0o777)
        self.get_avatar(
            logospath, teams['user_name'], teams['user_av_location'])
        self.get_avatar(logospath, teams['opp_name'], teams['opp_av_location'])

    def get_avatar(self, logospath, name, url):
        filename = os.path.join(logospath, '{0}.jpg'.format(name))
        if not os.path.exists(filename):
            debug.info('downloading avatar for {0}'.format(name))
            r = requests.get(url, stream=True)
            with open(filename, 'wb') as fd:
                for chunk in r.iter_content(chunk_size=128):
                    fd.write(chunk)

    def refresh_access_token(self):
        if not self.oauth.token_is_valid():
            self.oauth.refresh_access_token()
            self.oauth.session = self.oauth.oauth.get_session(
                token=self.oauth.access_token)
