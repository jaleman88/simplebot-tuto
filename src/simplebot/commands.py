import inspect
import types
from collections import OrderedDict
from typing import Callable, Dict, Generator, Optional, Set

from .hookspec import deltabot_hookimpl
from .templates import help_template

CMD_PREFIX = "/"
_cmds: Set[tuple] = set()


class NotFound(LookupError):
    """Command was not found."""


class Commands:
    def __init__(self, bot) -> None:
        self.logger = bot.logger
        self._cmd_defs: Dict[str, CommandDef] = OrderedDict()
        bot.plugins.add_module("commands", self)

    def register(
        self, func: Callable, name: str = None, help: str = None, admin: bool = False
    ) -> None:
        """register a command function that acts on each incoming non-system message.

        :param func: function can accept 'bot', 'command'(:class:`simplebot.commands.IncomingCommand`), 'message', 'payload' and 'replies'(:class:`simplebot.bot.Replies`) arguments.
        :param name: name of the command, example "/test", if not provided it is autogenerated from function name.
        :param help: command help, it will be extracted from the function docstring if not provided.
        :param admin: if True the command will be available for bot administrators only.
        """
        name = name or CMD_PREFIX + func.__name__
        if help is not None:
            func.__doc__ = help
        short, long, args = parse_command_docstring(
            func, args=["command", "replies", "bot", "payload", "args", "message"]
        )
        for cand_name in iter_underscore_subparts(name):
            if cand_name in self._cmd_defs:
                raise ValueError(
                    "command {!r} fails to register, conflicts with: {!r}".format(
                        name, cand_name
                    )
                )
        for reg_name in self._cmd_defs:
            if reg_name.startswith(name + "_"):
                raise ValueError(
                    "command {!r} fails to register, conflicts with: {!r}".format(
                        name, reg_name
                    )
                )

        cmd_def = CommandDef(
            name, short=short, long=long, func=func, args=args, admin=admin
        )
        self._cmd_defs[name] = cmd_def
        self.logger.debug("registered new command {!r}".format(name))

    def unregister(self, name: str) -> Callable:
        """unregister a command function by name."""
        return self._cmd_defs.pop(name)

    def dict(self) -> dict:
        return self._cmd_defs.copy()

    @deltabot_hookimpl
    def deltabot_incoming_message(self, bot, message, replies) -> Optional[bool]:
        if not message.text.startswith(CMD_PREFIX):
            return None
        args = message.text.split()
        payload = message.text.split(maxsplit=1)[1] if len(args) > 1 else ""
        orig_cmd_name = args.pop(0)

        if "@" in orig_cmd_name:
            suffix = "@" + bot.self_contact.addr
            if orig_cmd_name.endswith(suffix):
                orig_cmd_name = orig_cmd_name[: -len(suffix)]
            else:
                return True

        parts = orig_cmd_name.split("_")
        while parts:
            cmd_name = "_".join(parts)
            cmd_def = self._cmd_defs.get(cmd_name)
            if cmd_def is not None:
                break
            newarg = parts.pop()
            args.insert(0, newarg)
            payload = (newarg + " " + payload).rstrip()

        if not cmd_def or (
            cmd_def.admin and not bot.is_admin(message.get_sender_contact().addr)
        ):
            reply = "unknown command {!r}".format(orig_cmd_name)
            self.logger.warn(reply)
            if not message.chat.is_group():
                replies.add(text=reply)
            return True

        cmd = IncomingCommand(
            bot=bot, cmd_def=cmd_def, message=message, args=args, payload=payload
        )
        bot.logger.info("processing command {}".format(cmd))
        try:
            res = cmd.cmd_def(
                command=cmd,
                replies=replies,
                bot=bot,
                payload=cmd.payload,
                args=cmd.args,
                message=cmd.message,
            )
        except Exception as ex:
            self.logger.exception(ex)
        else:
            assert res is None, res
        return True

    @deltabot_hookimpl
    def deltabot_init(self, bot) -> None:
        self.register(func=self.command_help, name="/help")

    def command_help(self, bot, command, replies) -> None:
        """reply with help message about available commands."""
        is_admin = bot.is_admin(command.message.get_sender_contact().addr)
        cmds = []
        has_prefs = bool(bot.get_preferences())
        for c in self._cmd_defs.values():
            if not c.admin or is_admin:
                if c.cmd != "/set" or has_prefs:
                    cmds.append(c)
        pm = bot.plugins._pm
        plugins = [pm.get_name(plug) for plug, dist in pm.list_plugin_distinfo()]
        html = help_template.render(
            addr=bot.self_contact.addr, cmds=cmds, plugins=plugins
        )
        replies.add(text="ℹ️ Help", html=html)


class CommandDef:
    """Definition of a '/COMMAND' with args."""

    def __init__(self, cmd, short, long, func, args, admin=False) -> None:
        if cmd[0] != CMD_PREFIX:
            raise ValueError("cmd {!r} must start with {!r}".format(cmd, CMD_PREFIX))
        self.cmd = cmd
        self.long = long
        self.short = short
        self.func = func
        self.args = args
        self.admin = admin

    def __eq__(self, c) -> bool:
        return c.__dict__ == self.__dict__

    def __call__(self, **kwargs):
        for key in list(kwargs.keys()):
            if key not in self.args:
                del kwargs[key]
        return self.func(**kwargs)


class IncomingCommand:
    """incoming command request."""

    def __init__(self, bot, cmd_def, args, payload, message) -> None:
        self.bot = bot
        self.cmd_def = cmd_def
        self.args = args
        self.payload = payload
        self.message = message

    def __repr__(self) -> str:
        return "<IncomingCommand {!r} payload={!r} msg={}>".format(
            self.cmd_def.cmd[0], self.payload, self.message.id
        )


def parse_command_docstring(func, args) -> tuple:
    description = func.__doc__
    if not description:
        raise ValueError("{!r} needs to have a docstring".format(func))
    funcargs = set(inspect.getargs(func.__code__).args)
    if isinstance(func, types.MethodType):
        funcargs.discard("self")
    for arg in funcargs:
        if arg not in args:
            raise ValueError(
                "{!r} requests an invalid argument: {!r}, valid arguments: {!r}".format(
                    func, arg, args
                )
            )

    lines = description.strip().split("\n", maxsplit=1)
    return lines.pop(0), "".join(lines).strip(), funcargs


def iter_underscore_subparts(name) -> Generator[str, None, None]:
    parts = name.split("_")
    while parts:
        yield "_".join(parts)
        parts.pop()


def command_decorator(func: Callable = None, **kwargs) -> Callable:
    """Register decorated function as bot command.

    Check documentation of method `simplebot.commands.Commands.register` to
    see all parameters the decorated function can accept.
    """

    def _decorator(func) -> Callable:
        kwargs["func"] = func
        _cmds.add(tuple(sorted(kwargs.items())))
        return func

    if func is None:
        return _decorator
    return _decorator(func)
