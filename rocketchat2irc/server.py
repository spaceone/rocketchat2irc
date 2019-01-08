# -*- coding: utf-8 -*-

from argparse import ArgumentParser
from collections import defaultdict
from itertools import chain
from operator import attrgetter
from time import time
from logging import getLogger
from urlparse import urlparse

from circuits import BaseComponent, handler, Debugger
from circuits.net.events import close, write
from circuits.net.sockets import TCPServer
from circuits.protocols.irc import IRC, Message, joinprefix, reply, response
from circuits.protocols.irc.replies import (
	ERR_NICKNAMEINUSE, ERR_NOMOTD, ERR_NOSUCHCHANNEL, ERR_NOSUCHNICK,
	ERR_UNKNOWNCOMMAND, RPL_ENDOFNAMES, RPL_ENDOFWHO, RPL_NAMEREPLY,
	RPL_NOTOPIC, RPL_WELCOME, RPL_WHOREPLY, RPL_YOURHOST,
)

from rocketchat2irc.rocketchat import RocketChatClient

__version__ = "0.0.1"


class Channel(object):

	def __init__(self, name):
		self.name = name

		self.users = []


class User(object):

	def __init__(self, sock, host, port):
		self.sock = sock
		self.host = host
		self.port = port

		self.nick = None
		self.away = False
		self.channels = []
		self.signon = None
		self.registered = False
		self.userinfo = UserInfo()
		self.rc = None

	@property
	def prefix(self):
		userinfo = self.userinfo
		return joinprefix(self.nick, userinfo.user, userinfo.host)


class UserInfo(object):

	def __init__(self, user=None, host=None, name=None):
		self.user = user
		self.host = host
		self.name = name


class Server(BaseComponent):

	channel = 'server'
	network = "RocketChat2IRC"
	host = "localhost"
	version = "ircd v{0:s}".format(__version__)

	@classmethod
	def main(cls):
		parser = ArgumentParser()
		parser.add_argument('-b', '--bind', default='0.0.0.0:6667', help='Address to bind server socket to.')
		parser.add_argument('-d', '--debug', action='store_true')
		args = parser.parse_args()
		server = cls(args)
		server.run()
		return server

	def init(self, args):
		self.args = args

		self.logger = getLogger(__name__)
		if args.debug:
			self += Debugger(logger=self.logger)

		self.buffers = defaultdict(bytes)

		self.nicks = {}
		self.users = {}
		self.channels = {}
		if ':' in args.bind:
			address, port = args.bind.split(':', 1)
			port = int(port)
		else:
			address, port = args.bind, 6667

		self.transport = TCPServer(
			bind=(address, port),
			channel=self.channel
		).register(self)

		self.protocol = IRC(
			channel=self.channel,
			getBuffer=self.buffers.__getitem__,
			updateBuffer=self.buffers.__setitem__
		).register(self)

	def _notify(self, users, message, exclude=None):
		for user in users:
			if exclude is not None and user is exclude:
				continue
			self.fire(reply(user.sock, message))

	@handler('read')
	def read(self, sock, data):
		user = self.users[sock]
		host, port = user.host, user.port

		self.logger.info(
			"I: [{0:s}:{1:d}] {2:s}".format(host, port, repr(data))
		)

	@handler('write')
	def write(self, sock, data):
		user = self.users[sock]
		host, port = user.host, user.port

		self.logger.info(
			"O: [{0:s}:{1:d}] {2:s}".format(host, port, repr(data))
		)

	@handler('ready')
	def ready(self, server, bind):
		print("ircd v{0:s} ready! Listening on: {1:s}\n".format(__version__, "{0:s}:{1:d}".format(*bind)))

	@handler('connect')
	def connect(self, sock, host, port):
		self.users[sock] = User(sock, host, port)

		self.logger.info("C: [{0:s}:{1:d}]".format(host, port))

	@handler('disconnect')
	def disconnect(self, sock):
		if sock not in self.users:
			return

		user = self.users[sock]

		self.logger.info("D: [{0:s}:{1:d}]".format(user.host, user.port))

		nick = user.nick
		user, host = user.userinfo.user, user.userinfo.host

		yield self.call(
			response.create("quit", sock, (nick, user, host), "Leaving")
		)

		del self.users[sock]

		if nick in self.nicks:
			del self.nicks[nick]

	@handler('quit')
	def quit(self, sock, source, reason="Leaving"):
		user = self.users[sock]

		channels = [self.channels[channel] for channel in user.channels]
		for channel in channels:
			channel.users.remove(user)
			if not channel.users:
				del self.channels[channel.name]

		users = chain(*map(attrgetter("users"), channels))

		self.fire(close(sock))

		self._notify(
			users,
			Message("QUIT", reason, prefix=user.prefix), user
		)

	@handler('nick')
	def nick(self, sock, source, nick):
		user = self.users[sock]

		if nick in self.nicks:
			return self.fire(reply(sock, ERR_NICKNAMEINUSE(nick)))

		if not user.registered:
			user.registered = True
			self.fire(response.create("signon", sock, user))

		user.nick = nick
		self.nicks[nick] = user

	@handler('user')
	def user(self, sock, source, nick, user, host, name):
		_user = self.users[sock]

		_user.userinfo.user = user
		_user.userinfo.host = host
		_user.userinfo.name = name

		if _user.nick is not None:
			_user.registered = True
			self.fire(response.create("signon", sock, source))

	@handler('signon')
	def signon(self, sock, source):
		user = self.users[sock]
		if user.signon:
			return

		user.signon = time()

		self.fire(reply(sock, RPL_WELCOME(self.network)))
		self.fire(reply(sock, RPL_YOURHOST(self.host, self.version)))
		self.fire(reply(sock, ERR_NOMOTD()))

	def force_join(self, sock, source, channel):
		self.fire(response.create("join", sock, source, channel))

	@handler('join')
	def join(self, sock, source, name):
		user = self.users[sock]

		if name not in self.channels:
			channel = self.channels[name] = Channel(name)
		else:
			channel = self.channels[name]

		if user in channel.users:
			return

		user.channels.append(name)
		channel.users.append(user)

		self._notify(
			channel.users,
			Message("JOIN", name, prefix=user.prefix)
		)

		self.fire(reply(sock, RPL_NOTOPIC(name)))
		self.fire(reply(sock, RPL_NAMEREPLY(channel)))
		self.fire(reply(sock, RPL_ENDOFNAMES()))

	@handler('join', priority=2):
	def on_join(self, event, sock, source, name):
		user = self.users[sock]
		if not user.rc:
			return
		event.stop()
		user.rc.on_join(source, name)

	@handler('part')
	def part(self, sock, source, name, reason="Leaving"):
		user = self.users[sock]

		channel = self.channels[name]

		self._notify(
			channel.users,
			Message("PART", name, reason, prefix=user.prefix)
		)

		user.channels.remove(name)
		channel.users.remove(user)

		if not channel.users:
			del self.channels[name]

	@handler('part', priority=2):
	def on_join(self, event, sock, source, name, reason='Leaving'):
		user = self.users[sock]
		if not user.rc:
			return
		event.stop()
		user.rc.on_part(source, name, reason)

	@handler('privmsg')
	def privmsg(self, sock, source, target, message):
		user = self.users[sock]

		if target.startswith("#"):
			if target not in self.channels:
				return self.fire(reply(sock, ERR_NOSUCHCHANNEL(target)))

			channel = self.channels[target]

			self._notify(
				channel.users,
				Message("PRIVMSG", target, message, prefix=user.prefix),
				user
			)
		else:
			if target not in self.nicks:
				return self.fire(reply(sock, ERR_NOSUCHNICK(target)))

			self.fire(
				reply(
					self.nicks[target].sock,
					Message("PRIVMSG", target, message, prefix=user.prefix)
				)
			)

	@handler('privmsg', priority=1.5)
	def _on_privmsg_rc(self, event, sock, source, target, message):
		user = self.users[sock]
		if not user.rc:
			return
		event.stop()
		user.rc.on_privmsg(target, message)

	@handler('privmsg', priority=2)
	def _on_login(self, event, sock, source, target, message):
		if target.lower() != 'nickserv':
			return
		event.stop()
		if not message.startswith('identify '):
			return
		_, username, password = message.split()
		url = urlparse(username)
		username = url.username
		user = self.users[sock]
		user.rc = RocketChatClient(url, username, password)
		self += user.rc

	@handler('who')
	def who(self, sock, source, mask):
		if mask.startswith("#"):
			if mask not in self.channels:
				return self.fire(reply(sock, ERR_NOSUCHCHANNEL(mask)))

			channel = self.channels[mask]

			for user in channel.users:
				self.fire(reply(sock, RPL_WHOREPLY(user, mask, self.host)))
			self.fire(reply(sock, RPL_ENDOFWHO(mask)))
		else:
			if mask not in self.nicks:
				return self.fire(reply(sock, ERR_NOSUCHNICK(mask)))

			user = self.nicks[mask]

			self.fire(reply(sock, RPL_WHOREPLY(user, mask, self.host)))
			self.fire(reply(sock, RPL_ENDOFWHO(mask)))

	@handler('ping')
	def ping(self, event, sock, source, server):
		event.stop()
		self.fire(reply(sock, Message("PONG", server)))

	@handler('reply')
	def reply(self, target, message):
		user = self.users[target]

		if message.add_nick:
			message.args.insert(0, user.nick or "")

		if message.prefix is None:
			message.prefix = self.host

		self.fire(write(target, bytes(message)))

	@handler('list')
	def list(self, sock):
		user = self.users[sock]
		if not user.rc:
			return
		channel_list = user.rc.get_channels()
		self.fire(reply(sock, _M(u"321", "Channel Users Name")))
		for channel in channel_list:
			self.fire(reply(sock, RPL_LIST(channel['name'], channel['usersCount'], channel.get('topic', ''))))
		self.fire(reply(sock, RPL_LISTEND()))

	@property
	def commands(self):
		exclude = {"ready", "connect", "disconnect", "read", "write"}
		return list(set(self.events()) - exclude)

	@handler()
	def _on_event(self, event, *args, **kwargs):
		if event.name.endswith("_done"):
			return

		if isinstance(event, response):
			if event.name not in self.commands:
				event.stop()
				self.fire(reply(args[0], ERR_UNKNOWNCOMMAND(event.name)))
