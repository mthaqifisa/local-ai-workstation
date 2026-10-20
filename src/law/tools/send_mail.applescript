-- usage: osascript send_mail.applescript <to> <subject> <body-file> [attachment ...]
-- Sends through the Mail account already signed in on this Mac; LAW stores no mail password.
on run argv
	set toAddr to item 1 of argv
	set subj to item 2 of argv
	set bodyText to read (POSIX file (item 3 of argv)) as «class utf8»
	-- Resolve attachments here, not inside the Mail block: there Mail would try to interpret "POSIX file"
	-- itself and fail with "Can't get POSIX file" (-1728).
	set attachmentFiles to {}
	if (count of argv) > 3 then
		repeat with i from 4 to count of argv
			set end of attachmentFiles to ((POSIX file (item i of argv)) as alias)
		end repeat
	end if
	tell application "Mail"
		set msg to make new outgoing message with properties {subject:subj, content:bodyText & return & return, visible:false}
		tell msg
			make new to recipient at end of to recipients with properties {address:toAddr}
		end tell
		repeat with f in attachmentFiles
			tell content of msg
				make new attachment with properties {file name:(contents of f)} at after last paragraph
			end tell
		end repeat
		delay 2
		send msg
	end tell
	return "sent"
end run
