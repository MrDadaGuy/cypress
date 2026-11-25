-- Where new tickets are announced (Slack channel name or ID).
insert into org_settings (key, value) values ('board_slack_channel', '"#community-association-board"')
on conflict (key) do nothing;
