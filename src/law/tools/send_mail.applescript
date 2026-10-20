-- usage: osascript send_mail.applescript <to> <subject> <body-file> [attachment ...]
on run argv
	set toAddr to item 1 of argv
	set subj to item 2 of argv
	set bodyText to read (POSIX file (item 3 of argv)) as «class utf8»
	tell application "Mail"
		set msg to make new outgoing message with properties {subject:subj, content:bodyText & return & return, visible:false}
		tell msg
			make new to recipient at end of to recipients with properties {address:toAddr}
		end tell
		if (count of argv) > 3 then
			repeat with i from 4 to count of argv
				set f to POSIX file (item i of argv)
				tell content of msg
					make new attachment with properties {file name:(f as alias)} at after last paragraph
				end tell
			end repeat
		end if
		delay 2
		send msg
	end tell
	return "sent"
end run
