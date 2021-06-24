import discord


NO_TEXT = discord.PermissionOverwrite(
    view_channel=False,
    read_messages=False,
    send_messages=False,
    add_reactions=False,
)
SPECTATE_TEXT = discord.PermissionOverwrite(
    view_channel=True,
    read_messages=True,
    send_messages=False,
    add_reactions=False,
)
TEXT = discord.PermissionOverwrite(
    view_channel=True,
    read_messages=True,
    send_messages=True,
    add_reactions=True,
)
NO_VOICE = discord.PermissionOverwrite(
    view_channel=False,
    connect=False,
    speak=False,
    mute_members=False,
    priority_speaker=False,
)
SPECTATE_VOICE = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=False,
    priority_speaker=False,
    mute_members=False,
    deafen_members=False,
)
VOICE = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=True,
    priority_speaker=False,
    mute_members=False,
    deafen_members=False,
)
JUDGE_VOICE = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=True,
    priority_speaker=True,
    mute_members=True,
    deafen_members=True,
)
