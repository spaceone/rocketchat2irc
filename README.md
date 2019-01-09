# rocketchat2irc
A IRC server which forwards all messages between rocket-chat and your IRC client.

See also: https://github.com/RocketChat/Rocket.Chat/issues/1685

This is far away from being feature complete. Pull requests are very welcome.

## Install

* git clone https://github.com/circuits/circuits
* git clone https://github.com/spaceone/rocketchat2irc
* cd rocketchat2irc
* ln -s ../circuits/circuits

## Usage

1. Start the server via python -m rocketchat2irc -d -b 127.0.0.1:6667 (Make sure you bind to a local interface)
2. Connect with any IRC client to the server.
3. Type /msg nickserv identify ws://username:password@chat.example.com/websocket
