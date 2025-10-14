import requests
from datetime import datetime
# from utils import convert_time
import os
# import debug
import json
from yahoo_oauth import OAuth2

# https://fantasysports.yahooapis.com/fantasy/v2/games;game_codes=nfl I'm almost positive this is how to find the game id (406) but I totally forget now


class YahooFantasyInfo():
    def __init__(self, yahoo_consumer_key, yahoo_consumer_secret, game_id, league_id, team_id, week):
        self.team_id = team_id
        self.league_id = league_id
        self.game_id = game_id
        self.week = week
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
        return self.get_matchup(self.game_id, self.league_id, self.team_id, self.week)

    def refresh_scores(self):
        return self.get_matchup(self.game_id, self.league_id, self.team_id, self.week)

    def get_matchup(self, game_id, league_id, team_id, week):
        self.refresh_access_token()
        url = f"https://fantasysports.yahooapis.com/fantasy/v2/team/{self.game_id}.l.{self.league_id}.t.{self.team_id}/matchups;weeks={week}"
        response = self.oauth.session.get(url, params={'format': 'json'})
        data = response.json()
        # print(json.dumps(data, indent=2))  # Uncomment for debugging

        matchup = data["fantasy_content"]["team"][1]["matchups"]
        matchup_info = {}

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
                            matchup_info['user_name'] = nickname
                            matchup_info['user_av'] = nickname
                            matchup_info['user_av_location'] = image_url or logo_url
                            matchup_info['user_team'] = team_name
                            matchup_info['user_proj'] = projected_points
                            matchup_info['user_score'] = float(actual_points)
                        else:
                            matchup_info['opp_name'] = nickname
                            matchup_info['opp_av'] = nickname
                            matchup_info['opp_av_location'] = image_url or logo_url
                            matchup_info['opp_team'] = team_name
                            matchup_info['opp_proj'] = projected_points
                            matchup_info['opp_score'] = float(actual_points)

        return matchup_info

    def get_avatars(self, teams):
        self.refresh_access_token()
        # debug.info('getting avatars')
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
            # debug.info('downloading avatar for {0}'.format(name))
            r = requests.get(url, stream=True)
            with open(filename, 'wb') as fd:
                for chunk in r.iter_content(chunk_size=128):
                    fd.write(chunk)

    def refresh_access_token(self):
        if not self.oauth.token_is_valid():
            self.oauth.refresh_access_token()
            self.oauth.session = self.oauth.oauth.get_session(
                token=self.oauth.access_token)
