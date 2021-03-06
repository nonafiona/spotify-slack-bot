from subprocess import check_output
from slackclient import SlackClient
import re
import time
import json
import threading
import requests
import signal
import sys
import private_settings as settings
import spotify

def _get_song_artists(song):
    song_artists = []
    num_artists = len(song.artists)
    for artist in song.artists:
        song_artists.append(artist.name)
    if num_artists == 2:
        song_artists = " and ".join(song_artists)
    else:
        song_artists = (", ".join(song_artists))[::-1].replace(", "[::-1], ", and "[::-1], 1)[::-1]
    return song_artists

def _get_song_data(song):
    return dict(song_id = str(song.link), song_name = song.name, song_artists = _get_song_artists(song))

def _get_recommendations(song_id):
    REQUEST_URL = "https://djlamp.herokuapp.com/api/recommend?id=%s" % song_id
    response = json.loads(requests.get(REQUEST_URL).content)
    if response.has_key("results"):
        return response["results"]
    print "last.fm ERROR: No recommendations for " + str(song_id)
    return []

class SpotifySlackBot():
    def __init__(self, api_key, broadcast_channel, dev):
        self.broadcast_channel = broadcast_channel
        self.sc = SlackClient(api_key)
        self.session = spotify.Session()
        self.is_dev = dev
        
        logged_in_event = threading.Event()
        def connection_state_listener(session):
            if session.connection.state is spotify.ConnectionState.LOGGED_IN:
                logged_in_event.set()
        
        loop = spotify.EventLoop(self.session)
        loop.start()
        self.session.on(
            spotify.SessionEvent.CONNECTION_STATE_UPDATED,
            connection_state_listener)
        self.session.login(settings.SPOTIFY_USERNAME, settings.SPOTIFY_PASSWORD)
        logged_in_event.wait()
        loop.stop()
        
        # Get the user list
        response = self.sc.api_call('users.list')
        self.users = json.loads(response)['members']
        self.song_queue = []
        self.auto_queue = []
        self.recommendations_broken = False;

    def command_help(self, event):
            self.sc.rtm_send_message(event['channel'],
                                     "Hey, how are you?  I'm here to help you control our office music!\n"
                                     "I can give you some information about the player, the song that is playing now, and the songs that will play afterwards with the following commands:\n"
                                     "- `song` or `current`: I'll tell you which song is playing and who is the artist.\n"
                                     "- `requests` or `queue`: I'll tell you all the songs in the queue.\n"
                                     "- `volume`: I'll tell you the current sound volume of the player (0 is minimum, 100 is maximum).\n"
                                     "\n"
                                     "I can also control playback and take requests, with the following commands:\n"
                                     "- `play`: I'll resume playback of the playlist, if it is paused.\n"
                                     "- `pause`: I'll pause the playback of the playlist, if it is playing.\n"
                                     "- `skip` or `next`: I'll skip the current song and play another one.\n"
                                     "- `request SONG`, `queue SONG`, or `play SONG`: I'll search Spotify for a song that matches your SONG query and then add it to the song queue.\n"
                                     "- `remove NUMBER`: I'll remove the queued song in the position specified by NUMBER from the song queue (only works for songs you requested).\n"
                                     "\n"
                                     "*Please note:* When you give commands for me to control playback and take requests, *I'll advertise on #%s that you asked me to do it*,"
                                     " just so everyone knows what is going on. Please use these only if you really need to :)"
                                        % (self.broadcast_channel)
            )

    def command_current_song(self, event):
        data = self.run_spotify_script('current-song','')
        data = data.strip().split('\n')
        data = {"id": data[0], "name": data[1], "artist": data[2]}
        message = u"Hey, the current song is *%s* by *%s*. You can open it on Spotify: %s" % (data['name'], data['artist'], data['id'])
        
        self.sc.rtm_send_message(event['channel'], message)
        
    def command_playback_play(self, event):
        self.run_spotify_script('playback-play','')
        self.sc.rtm_send_message(event['channel'], "Sure, let the music play!")
        self.sc.rtm_send_message(self.broadcast_channel, "*Resumed playback*, as requested by %s." % (self.get_username(event['user'])))

    def command_playback_pause(self, event):
        self.run_spotify_script('playback-pause','')
        self.sc.rtm_send_message(event['channel'], "Alright, let's have some silence for now.")
        self.sc.rtm_send_message(self.broadcast_channel, "*Paused playback*, as requested by %s." % (self.get_username(event['user'])))

    def command_playback_skip(self, event):
        self.run_spotify_script('playback-skip','')
        self.sc.rtm_send_message(event['channel'], "Sure, let's listen to something else.")
        self.sc.rtm_send_message(self.broadcast_channel, "*Skipping this song*, as requested by %s." % (self.get_username(event['user'])))
        self.play_next_song()

    def command_current_volume(self, event):
        volume = int(self.run_spotify_script('current-volume','').strip())
        self.sc.rtm_send_message(event['channel'], "The current sound volume is *%s/100*" % volume)

    def command_show_queue(self, event):
        message =  "*Song Queue:*\n"
        num_songs = len(self.song_queue)
        if not self.song_queue:
            message += "\tEMPTY! Request a song! DJ Lamp will spin the discs in the meantime ;)"
        else:
            for number, (song, requester, requester_channel) in enumerate(self.song_queue):
                if(number > 9):
                    num_additional_songs = num_songs - number
                    message += "\t\t...%s more song" % num_additional_songs
                    if num_additional_songs > 1:
                        message += "s"
                    message += "..."
                    break
                song_data = _get_song_data(song)
                requester = self.get_username(requester)
                message += u"\t*%s*. *%s* by *%s* (%s) - requested by %s\n" % (number + 1, song_data['song_name'], song_data['song_artists'], song_data['song_id'], requester)
        self.sc.rtm_send_message(event['channel'], message)

    def command_queue_song(self, event):
        song_query = " ".join(event['text'].split()[1:])
        search = self.session.search(query=song_query)
        search.load()
        songs = search.tracks
        if not songs:
            self.sc.rtm_send_message(event['channel'], "Hey there! Sorry, I can't seem to find that song. Please try another.")
        else:
            position = len(self.song_queue) + 1
            song = songs[0]
            song_data = _get_song_data(song)
            requester = self.get_username(event['user'])
            message = u"%s added *%s* by *%s* (%s) to the song queue." % (requester, song_data['song_name'], song_data['song_artists'], song_data['song_id'])
            self.sc.rtm_send_message(event['channel'], "Sure, added *%s* by *%s* (%s) to the queue (*#%s*)." % (song_data['song_name'], song_data['song_artists'], song_data['song_id'], position))
            self.sc.rtm_send_message(self.broadcast_channel, message)
            self.song_queue.append((song, event['user'], event['channel']))
            self.recommendations_broken = False

    def command_remove_from_queue(self, event):
        number = int(event['text'].split()[1])
        message = ""
        if number > len(self.song_queue):
            message = "Sorry, there is no song #*%s* on the queue. Type in a different number." % number
        else:
            index = number - 1
            (song, requester, requester_channel) = self.song_queue[index]
            song_data = _get_song_data(song)
            if event['user'] != requester:
                message = u"Sorry, you didn't request song #*%s*. *%s* by *%s*, so you can't remove it. Type in a different number." % (number, song_data['song_name'], song_data['song_artists'])
            else:
                self.song_queue.pop(index)
                message = u"Sure, I'll remove song #*%s*. *%s* by *%s* from the queue." % (number, song_data['song_name'], song_data['song_artists'])
                self.sc.rtm_send_message(self.broadcast_channel,
                                         u"%s removed song #*%s*. *%s* by *%s* from the queue." % (self.get_username(requester), number, song_data['song_name'], song_data['song_artists']))     
        self.sc.rtm_send_message(event['channel'], message)

    def play_next_song(self):
        if self.song_queue:
            self.auto_queue = []
            (song, requester, requester_channel) = self.song_queue.pop(0)
            song_data = _get_song_data(song)
            requester = self.get_username(requester)
            message = u"Now playing *%s* by *%s* , as requested by %s. You can open it on Spotify: %s" % (song_data['song_name'], song_data['song_artists'], requester, song_data['song_id'])

            self.run_spotify_script('play-song', song_data['song_id'])
            self.sc.rtm_send_message(requester_channel, u"Now playing the song you requested: *%s* by *%s (%s)*!" % (song_data['song_name'], song_data['song_artists'], song_data['song_id']))
            self.sc.rtm_send_message(self.broadcast_channel, message)
            if not self.song_queue:
                self.sc.rtm_send_message(self.broadcast_channel, "Hey, everyone, there are no more songs in the queue. After this song ends, I'll be playing my own mix until someone requests a song.")
        else:
            if not self.auto_queue:
                self.auto_queue = self.auto_queue_songs()
            if not self.auto_queue:
                self.sc.rtm_send_message(self.broadcast_channel, "Hey, everyone, I can't seem to access my DJ Lamp mix :(. It's probably an internet issue that should be fixed shortly. I can still take requests though, so tell me what you wanna hear!")
                self.recommendations_broken = True
                return
            song = self.auto_queue.pop(0)
            song_query = song['artist'] + " " + song['title']
            search = self.session.search(query=song_query)
            search.load()
            songs = search.tracks
            if not songs:
                song_query = song['artist'].replace(" & ", ", ") + " " + song['title']
                search = self.session.search(query=song_query)
                search.load()
                songs = search.tracks
            if not songs:
                self.play_next_song()
            else:
                song = songs[0]
                song_data = _get_song_data(song)
                message = u"Now playing *%s* by *%s* as part of my DJ Lamp mix. You can open the song on Spotify: %s" % (song_data['song_name'], song_data['song_artists'], song_data['song_id'])
                self.run_spotify_script('play-song', song_data['song_id'])
                self.sc.rtm_send_message(self.broadcast_channel, message)

    def auto_queue_songs(self):
        data = self.run_spotify_script('current-song','')
        seed_song_id = data.strip().split('\n')[0]
        return _get_recommendations(seed_song_id)

    def command_unknown(self, event):
        self.sc.rtm_send_message(event['channel'], "Hey there! I kinda didn't get what you mean, sorry. If you need, just say `help` and I can tell you how I can be of use. ;)")

    def run_spotify_script(self, *args):
        return check_output(['./spotify.applescript'] + list(args))

    def get_player_position(self):
        output = check_output(['./checkposition.applescript']).strip().split("\n")
        output[0] = float(output[0]) / 1000
        output[1] = output[1]
        return output

    def get_username(self, user_id):
        for user in self.users:
            if user['id'] == user_id:
                return '@%s' % user['name']
        return 'someone'
    
    def run(self):
        commands = [
            ('hey|help', self.command_help),
            ('song|current', self.command_current_song),
            ('play$', self.command_playback_play),
            ('pause', self.command_playback_pause),
            ('skip|next', self.command_playback_skip),
            ('volume$', self.command_current_volume),
            ('queue$|requests$', self.command_show_queue),
            ('play .+|queue .+|request .+', self.command_queue_song),
            ('remove [1-9]([0-9])*', self.command_remove_from_queue),
            ('.+', self.command_unknown)
        ]
        
        if self.sc.rtm_connect():
            print("DJ Lamp is online!")
            self.sc.rtm_send_message(self.broadcast_channel, "Hey, everyone, DJ Lamp is now online! I'll be playing my own mix until someone requests a song. Just send me a message (`hey` or `help` for help)!")
            while True:
                events = self.sc.rtm_read()
                for event in events:
                    print event
                    if event.get('type') == 'message' and event.get('channel')[0] == 'D':
                        for (expression, function) in commands:
                            if event.has_key('text') and re.match(expression, event['text'], re.IGNORECASE):
                                function(event)
                                break
                try:
                    position = self.get_player_position()
                    if self.is_dev:
                        print position
                except ValueError:
                    position = [0, 'paused']
                    time.sleep(3)
                if position == [0, 'paused'] and not self.recommendations_broken:
                    self.play_next_song()
                time.sleep(1)
        else:
            print("\rDJ Lamp aborted")
            sys.exit(0)

if __name__ == '__main__':
    print("DJ Lamp starting up...")
    try:
        channel = ""
        dev = False
        if len(sys.argv) >= 2 and sys.argv[1] == 'dev':
            channel = "private-test-dj-lamp"
            dev = True
        else:
            channel = settings.SPOTIFYSLACK_SLACK_BROADCAST_CHANNEL
        bot = SpotifySlackBot(settings.SPOTIFYSLACK_SLACK_API_KEY, channel, dev)
    except KeyboardInterrupt:
        print("\rDJ Lamp aborted")
        sys.exit(0)
        
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\rDJ Lamp signing off!")
        bot.sc.rtm_send_message(bot.broadcast_channel, "Hey, everyone, DJ Lamp signing off! See ya next time!")
        sys.exit(0)