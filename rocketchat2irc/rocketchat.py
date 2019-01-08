# -*- coding: utf-8 -*-

import json
import time
import hashlib
from getpass import getpass

from circuits import handler, Debugger
from circuits.io import stdin
from circuits.net.events import write
from circuits.web.websockets import WebSocketClient


class RocketChatError(Exception):
	pass


class RocketChatClient(WebSocketClient):

	def __init__(self, uri, username, password, user=None):
		channel = 'irc-rc-%s' % (username,)
		self.username = username
		self.password = password
		self.user = user
		super(RocketChatClient, self).__init__(uri, channel=channel, wschannel='ws-%s' % (channel,))

	def init(self, *args, **kwargs):
		self.pw_hash = hashlib.sha256(self.password.encode()).hexdigest()
		self.logged_in = False
		self.stack = {}

	def writej(self, data):
		return self.fire(write(json.dumps(data)), self.wschannel)

	@handler('read')
	def _on_receive(self, event, data):
		if self._wschannel not in event.channels:
			return

		response = None
		try:
			data = json.loads(data)
		except ValueError:
			return
		if not isinstance(data, dict):
			return

		if not self.logged_in:
			self.logged_in = 2
			self.writej(self._rc_connect_query())
			self.writej(self._rc_login_query())
		if self.logged_in == 2 and 'id' in data.get('result', {}):
			self.logged_in = True
			self.user_id = data['result']['id']
			self.on_logged_in()

		if data.get('msg') == 'ping':
			response = {'msg': 'pong'}
		#elif response.get('msg') == 'changed':
		#	pass
		elif 'id' in data:
			self.stack[data['id']] = data

		response = self.handle_message(data)

		if response is not None:
			self.writej(response)

	def on_logged_in(self):
		channels = self.rc.get_joined_channels()
		for channel in channels:
			self.irc.force_join(sock, source, '#%s' % (channel['name'],))

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

	def get_joined_channels(self):
		query = {
			'msg': 'method',
			'method': 'rooms/get',
			'id': str(time.time()),
		}
		result = yield (yield self.rc_get(query)['result'])
		yield [room for room in result if 'name' in room]

	def get_users_of_room(self, room_id):
		include_offline = False
		query = {
			'msg': 'method',
			'method': 'getUsersOfRoom',
			'id': str(time.time()),
			'params': [room_id, include_offline],
		}
		yield (yield self.rc_get(query)['result'])

	def get_channel_info(self, room_name):
		room_type = 'c'
		query = {
			'msg': 'method',
			'method': 'getRoomByTypeAndName',
			'id': str(time.time()),
			'params': [room_type, room_name],
		}
		try:
			yield self.rc_get(query)['result']
		except RocketChatError:  # FIXME
			query['params'] = ['p', room_name]
			yield self.rc_get(query)['result']

	def get_channel_name(self, room_id):
		query = {
			'msg': 'method',
			'method': 'getRoomNameById',
			'id': str(time.time()),
			'params': [room_id, ],
		}
		yield self.rc_get(query)['result']

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
		yield self.rc_get(query)['result']['results']

	def get_private_room_id(self, nick):
		query = {
			'msg': 'method',
			'method': 'createDirectMessage',
			'id': str(time.time()),
			'params': [nick, ],
		}
		yield self.rc_get(query)['result']

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
		yield self.rc_get(query)['result']

	def leave_room(self, room_id):
		query = {
			'msg': 'method',
			'method': 'leaveRoom',
			'id': str(time.time()),
			'params': [room_id],
		}
		yield self.rc_get(query)

	def join_room(self, room_id):
		query = {
			'msg': 'method',
			'method': 'joinRoom',
			'id': str(time.time()),
			'params': [room_id],
		}
		yield self.rc_get(query)

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

	def unsub_to_room_messages(self, room_id):
		#  subscriptions/get' returns the wrong id????
		#  -> use a cache...
		if room_id in self.__sub_room2id_cache:
			query = {
				'msg': 'unsub',
				'id': self.__sub_room2id_cache[room_id],
			}
			self.writej(query)

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

	def _rc_get(self, query):
		assert 'id' in query
		self.writej(query)
		yield self.wait('read')
		while query['id'] not in self.stack:
			return
		result = self.stack.pop(query['id'])
		if 'error' in result:
			raise RocketChatError(
				error=result['error']['error'],
				reason=result['error']['reason']
			)
		yield result

	def handle_message(self, data):
		if data["collection"] == "stream-room-messages":
			msg = data["fields"]["args"][0]
			if msg.get('t') == "uj":
				self.send_join(msg)
			elif msg.get('t') == "ul":
				self.send_part(msg)
			elif msg.get("attachments"):
				self.send_file_link(msg)
			else:
				self.send_irc_message(msg)

		if data["collection"] == "stream-notify-user":
			msg = data["fields"]["args"][0]["payload"]
			if "type" in msg and msg["type"] == "d":
				self.send_private_message(data["fields"]["args"][0]["payload"])

	def send_irc_message(self, msg):
		if msg["u"]["_id"] == self.user_id:
			return
		channel_name = self.get_channel_name(msg["rid"])
		for line in msg["msg"].splitlines():
			self.irc.privmsg(msg["u"]["username"], None, '#%s' % (channel_name,), line)

	def send_file_link(self, msg):
		if msg["u"]["_id"] == self.user_id:
			return
		if "title_link" not in msg["attachments"][0]:
			self.send_irc_message(msg)
			return
		channel_name = self.get_channel_name(msg["rid"])
		lines = "{description}: https:{rc_server}{link}".format(
			description=msg["attachments"][0].get("description", ""),
			rc_server=self.server.split(":", 1)[-1],  # FIXME
			link=msg["attachments"][0]["title_link"]
		)
		for line in lines.splitlines():
			self.irc.privmsg(msg["u"]["username"], None, '#%s' % (channel_name,), line)

	def send_join(self, msg):
		channel_name = self.get_channel_name(msg["rid"])
		self.irc.force_join(self.sock, self.get_source(msg["u"]["username"]), '#%s' % (channel_name,))

	def get_source(self, username):
		return [username, '%s!%s@%s' % (username, username, 'localhost')]

	def send_part(self, msg):
		channel_name = self.get_channel_name(msg["rid"])
		self.irc.force_part(self.sock, self.get_source(msg["u"]["username"]), '#%s' % (channel_name,))

	def send_private_message(self, msg):
		for line in msg["message"]["msg"].split("\n"):
			self.irc.privmsg(self.sock, self.get_source(msg["sender"]["username"]), self.user.nick, line)

	def on_privmsg(self, target, msg):
		if target.startswith("#"):
			channel_info = self.get_channel_info(
				target.replace("#", "", 1)
			)
			rid = channel_info["_id"]
		else:
			rid = self.get_private_room_id(target)["rid"]

		self.send_irc_message(rid, msg)

	def on_join(self, source, name):
		channel_info = self.get_channel_info(
			name.replace("#", "", 1)
		)
		channel_members = self.get_users_of_room(
			channel_info["_id"]
		)
		self.join_room(channel_info["_id"])
		self.sub_to_room_messages(channel_info["_id"])
		users = " ".join([
			member["username"] for member in channel_members["records"]
		])
		self.irc.join(self.user.sock, source, name)

	def on_part(self, source, name, reason):
		channel_info = self.get_channel_info(
			name.replace("#", "", 1)
		)
		self.unsub_to_room_messages(channel_info["_id"])
		self.leave_room(channel_info["_id"])
		self.irc.part(self.user.sock, source, name, reason)

	@handler("read", channel="stdin")
	def stdin_read(self, data):
		user, message = data.split(None, 1)
		exec(message)


if __name__ == '__main__':
	url = raw_input('url: ')
	username = raw_input('username: ')
	password = getpass('password: ')
	chat = RocketChatClient(url, username, password)
	chat += Debugger()
	chat += stdin
	chat.run()
