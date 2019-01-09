# -*- coding: utf-8 -*-

import json
import time
import hashlib
from getpass import getpass

from circuits import handler, Debugger, Event
from circuits.io import stdin
from circuits.net.events import write
from circuits.web.websockets import WebSocketClient

from rocketchat2irc.server import Channel, User


class RocketChatError(Exception):
	pass


class RocketChatClient(WebSocketClient):

	def __init__(self, uri, username, password, user=None):
		channel = 'irc-rc-%s' % (username,)
		self.username = username
		self.pw_hash = hashlib.sha256(password.encode()).hexdigest()
		self.user = user
		super(RocketChatClient, self).__init__(uri, channel=channel, wschannel='ws-%s' % (channel,))

	def init(self, *args, **kwargs):
		self.logged_in = False
		self.stack = {}

	def writej(self, data):
		return self.fire(write(json.dumps(data)), self._wschannel)

	def call_writej(self, data):
		return self.call(write(json.dumps(data)), self._wschannel)

	@handler('read', channel='*')
	def _on_receive(self, event, *data):
		if self._wschannel not in event.channels:
			return

		response = None
		try:
			data = json.loads(data[0])
		except ValueError:
			return
		if not isinstance(data, dict):
			return

		if 'id' in data:
			self.stack[data['id']] = data

		response = self.handle_message(data)

		if response is not None:
			self.writej(response)

	def handle_message(self, data):
		if not self.logged_in and data.get('server_id'):
			# {"server_id":"0"}
			return self._rc_connect_query()
		if not self.logged_in and data.get('msg') == 'connected':
			# {"msg":"connected","session":"XXXXXX"}
			return self._rc_login_query()
		if not self.logged_in and data.get('msg') == 'result':
			# {"msg":"added","collection":"users","id":"XXX","fields":{"username":"xxx","emails":[{"address":"x@x.de","verified":true}]}}
			# {"msg":"result","id":"0","result":{"id":"XXX","token":"XXXX","tokenExpires":{"$date":1554807371206},"type":"password"}}
			# {"msg":"updated","methods":["0"]}
			self.logged_in = True
			self.user_id = data['result']['id']
			self.fire(Event.create('logged_in'))
		if data.get('msg') == 'ping':
			# {"msg":"ping"}
			return {'msg': 'pong'}
		#if data.get('msg') == 'changed':
		#	pass

		if data.get("collection") == "stream-room-messages":
			msg = data["fields"]["args"][0]
			if msg.get('t') == "uj":
				self.fire(Event.create('send_join', msg))
			elif msg.get('t') == "ul":
				self.fire(Event.create('send_part', msg))
			elif msg.get("attachments"):
				self.fire(Event.create('send_file_link', msg))
			else:
				self.fire(Event.create('send_irc_message', msg))

		if data.get("collection") == "stream-notify-user":
			msg = data["fields"]["args"][0]["payload"]
			if "type" in msg and msg["type"] == "d":
				self.fire(Event.create('send_private_message', data["fields"]["args"][0]["payload"]))

	@handler('disconnected')
	def on_disconnect(self):
		self.user.rc = None
		# TODO: disconnect user from IRC server
		self.unregister()

	def _rc_connect_query(self):
		return {
			'msg': 'connect',
			'version': '1',
			'support': ['1'],
		}

	def _rc_login_query(self):
		return {
			'msg': 'method',
			'method': 'login',
			'id': '0',
			'params': [{
				'user': {'username': self.username},
				'password': {
					'digest': self.pw_hash,
					'algorithm': 'sha-256'
				}
			}]
		}

	@handler('logged_in')
	def on_logged_in(self):
		channels = (yield self.call(Event.create('get_channels'))).value
		for cnl in channels:
			channel = Channel(cnl['name'])
			channel.topic = cnl.get('topic')
			self.user.irc.channels[channel.name] = channel

		channels = (yield self.call(Event.create('get_joined_channels'))).value
		for channel in channels:
			self.user.irc.force_join(self.user.sock, self.user.source, '#%s' % (channel['name'],))

	@handler('get_joined_channels')
	def get_joined_channels(self):
		query = {
			'msg': 'method',
			'method': 'rooms/get',
			'id': str(time.time()),
		}
		for _ in self.rc_get(query):
			if isinstance(_, list):
				yield [room for room in _ if 'name' in room]
			else:
				yield _

	@handler('get_users_of_room')
	def get_users_of_room(self, room_id):
		include_offline = False
		query = {
			'msg': 'method',
			'method': 'getUsersOfRoom',
			'id': str(time.time()),
			'params': [room_id, include_offline],
		}
		for _ in self.rc_get(query):
			yield _

	@handler('get_channel_info')
	def get_channel_info(self, room_name):
		room_type = 'c'
		query = {
			'msg': 'method',
			'method': 'getRoomByTypeAndName',
			'id': str(time.time()),
			'params': [room_type, room_name],
		}
		for _ in self.rc_get(query):
			if isinstance(_, RocketChatError):
				query['params'] = ['p', room_name]
				for _ in self.rc_get(query):
					yield _
			else:
				yield _

	@handler('get_channel_name')
	def get_channel_name(self, room_id):
		query = {
			'msg': 'method',
			'method': 'getRoomNameById',
			'id': str(time.time()),
			'params': [room_id, ],
		}
		for _ in self.rc_get(query):
			yield _

	@handler('get_channels')
	def get_channels(self):
		query = {
			'msg': 'method',
			'method': 'browseChannels',
			'id': str(time.time()),
			'params': [{
				'text': '',
				'type': 'channels',
				'sortBy': 'usersCount',
				'sortDirection': 'desc',
				'limit': 333,
				'page': 0
			}]
		}
		for _ in self.rc_get(query):
			if isinstance(_, dict) and 'results' in _:
				yield _['results']
			else:
				yield _

	@handler('get_private_room_id')
	def get_private_room_id(self, nick):
		query = {
			'msg': 'method',
			'method': 'createDirectMessage',
			'id': str(time.time()),
			'params': [nick, ],
		}
		for _ in self.rc_get(query):
			yield _

	@handler('send_message')
	def send_message(self, room_id, msg):
		query = {
			'msg': 'method',
			'method': 'sendMessage',
			'id': str(time.time()),
			'params': [{
				'rid': room_id,
				'msg': msg,
			}]
		}
		for _ in self.rc_get(query):
			yield _

	@handler('leave_room')
	def leave_room(self, room_id):
		query = {
			'msg': 'method',
			'method': 'leaveRoom',
			'id': str(time.time()),
			'params': [room_id],
		}
		for _ in self.rc_get(query):
			yield _

	@handler('join_room')
	def join_room(self, room_id):
		query = {
			'msg': 'method',
			'method': 'joinRoom',
			'id': str(time.time()),
			'params': [room_id],
		}
		for _ in self.rc_get(query):
			yield _

	@handler('sub_to_room_messages')
	def sub_to_room_messages(self, room_id):
		query = {
			'msg': 'sub',
			'id': str(time.time()),
			'name': 'stream-room-messages',
			'params': [
				room_id,
				False,
			],
		}
		self.__sub_room2id_cache[room_id] = query['id']
		self.writej(query)

	@handler('unsub_to_room_messages')
	def unsub_to_room_messages(self, room_id):
		#  subscriptions/get' returns the wrong id????
		#  -> use a cache...
		if room_id in self.__sub_room2id_cache:
			query = {
				'msg': 'unsub',
				'id': self.__sub_room2id_cache[room_id],
			}
			self.writej(query)

	@handler('sub_to_user_notifications')
	def sub_to_user_notifications(self):
		query = {
			'msg': 'sub',
			'id': str(time.time()),
			'name': 'stream-notify-user',
			'params': [
				'{}/notification'.format(self.user_id),
				False,
			],
		}
		self.writej(query)

	def rc_get(self, query):
		assert 'id' in query
		yield self.call_writej(query)
		print 'CALLED'
		#yield self.wait('read')
		#print 'READ'
		while query['id'] not in self.stack:
			print 'ID not in stack'
			yield
		result = self.stack.pop(query['id'])
		if 'error' in result:
			yield RocketChatError(result['error']['error'], result['error']['reason'])
			return
		yield result['result']

	@handler('send_irc_message')
	def send_irc_message(self, msg):
		if msg["u"]["_id"] == self.user_id:
			return
		channel_name = (yield self.call(Event.create('get_channel_name', msg["rid"]))).value
		user = self.get_fake_user(msg["u"]["username"])
		for line in msg["msg"].splitlines():
			self.user.irc.privmsg(user, user.source, '#%s' % (channel_name,), line)

	@handler('send_file_link')
	def send_file_link(self, msg):
		if msg["u"]["_id"] == self.user_id:
			return
		if "title_link" not in msg["attachments"][0]:
			self.fire(Event.create('send_irc_message', msg))
			return
		channel_name = (yield self.call(Event.create('get_channel_name', msg["rid"]))).value
		lines = "{description}: https:{rc_server}{link}".format(
			description=msg["attachments"][0].get("description", ""),
			rc_server=self.server.split(":", 1)[-1],  # FIXME
			link=msg["attachments"][0]["title_link"]
		)
		user = self.get_fake_user(msg["u"]["username"])
		for line in lines.splitlines():
			self.user.irc.privmsg(user, user.source, '#%s' % (channel_name,), line)

	@handler('send_join')
	def send_join(self, msg):
		channel_name = (yield self.call(Event.create('get_channel_name', msg["rid"]))).value
		user = self.get_fake_user(msg["u"]["username"])
		self.user.irc.force_join(self.sock, user, '#%s' % (channel_name,))

	def get_source(self, username):
		return [username, '%s!%s@%s' % (username, username, 'localhost')]

	@handler('send_part')
	def send_part(self, msg):
		channel_name = (yield self.call(Event.create('get_channel_name', msg["rid"]))).value
		user = self.get_fake_user(msg["u"]["username"])
		self.user.irc.force_part(self.sock, user.source, '#%s' % (channel_name,))

	@handler('send_private_message')
	def send_private_message(self, msg):
		user = self.get_fake_user(msg["sender"]["username"])
		for line in msg["message"]["msg"].split("\n"):
			self.user.irc.privmsg(self.sock, user.source, self.user.nick, line)

	def get_fake_user(self, username):
		user = User(None, None, 'localhost', 6667)
		user.nick = username
		user.userinfo.user = username
		user.userinfo.host = 'localhost'
		user.userinfo.name = username
		self.user.irc.users[user] = user
		return user

	@handler('privmsg')
	def on_privmsg(self, target, msg):
		if target.startswith("#"):
			channel_info = (yield self.call(Event.create('get_channel_info', target.replace("#", "", 1)))).value
			rid = channel_info["_id"]
		else:
			channel_info = (yield self.call(Event.create('get_private_room_id', target))).value
			rid = channel_info["_id"]

		self.fire(Event.create('send_message', rid, msg))

	@handler('join')
	def on_join(self, source, name):
		channel_info = (yield self.call(Event.create('get_channel_info', name.replace("#", "", 1)))).value
		channel_members = (yield self.call(Event.create('get_users_of_room', channel_info["_id"]))).value
		self.join_room(channel_info["_id"])
		self.sub_to_room_messages(channel_info["_id"])
		users = " ".join(member["username"] for member in channel_members["records"])
		self.user.irc.join(self.user.sock, source, name)

	@handler('part')
	def on_part(self, source, name, reason):
		channel_info = (yield self.call(Event.create('get_channel_info', name.replace("#", "", 1)))).value
		yield self.call(Event.create('unsub_to_room_messages', channel_info["_id"]))
		yield self.call(Event.create('leave_room', channel_info["_id"]))
		self.user.irc.part(self.user.sock, source, name, reason)

	@handler('list')
	def on_list(self):
		channels = (yield self.call(Event.create('get_channels'))).value
		channel_list = [(channel['name'], channel['usersCount'], channel.get('topic', '')) for channel in channels]
		self.user.irc.send_channel_list(self.user.sock, channel_list)

	@handler("read", channel="stdin")
	def stdin_read(self, data):
		#user, message = data.split(None, 1)
		message = data
		exec(message.strip())


if __name__ == '__main__':
	url = raw_input('url: ')
	username = raw_input('username: ')
	password = getpass('password: ')
	chat = RocketChatClient(url, username, password)
	chat += Debugger()
	chat += stdin
	chat.run()
