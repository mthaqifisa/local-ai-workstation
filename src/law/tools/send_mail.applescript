-- usage: osascript send_mail.applescript <to> <subject> <body-file> [attachment ...]
-- The body file has one paragraph per line, "style<TAB>text" (law.mail_format). Mail cannot be scripted with an
-- HTML body, but its rich-text body can be styled per paragraph, and Mail sends that as HTML with a plain-text
-- part. Sends through the account already signed in on this Mac; LAW stores no mail password.
on run argv
	set toAddr to item 1 of argv
	set subj to item 2 of argv
	set raw to read (POSIX file (item 3 of argv)) as «class utf8»
	set styleList to {}
	set textList to {}
	set AppleScript's text item delimiters to linefeed
	set rowList to text items of raw
	repeat with r in rowList
		set r to contents of r
		if length of r > 0 then
			set AppleScript's text item delimiters to tab
			set parts to text items of r
			set end of styleList to item 1 of parts
			if (count of parts) > 1 then
				set end of textList to (items 2 thru -1 of parts) as text
			else
				set end of textList to ""
			end if
		end if
	end repeat
	set AppleScript's text item delimiters to return
	set bodyText to textList as text
	set AppleScript's text item delimiters to ""
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
		try -- styling is presentation only: if it fails, the clean text still goes
			repeat with i from 1 to count of styleList
				set pStyle to item i of styleList
				set {fnt, sz, col} to {"HelveticaNeue", 14, {7710, 8995, 9766}}
				if pStyle is "title" then
					set {fnt, sz, col} to {"HelveticaNeue-Bold", 24, {2816, 15616, 11776}}
				else if pStyle is "subtitle" then
					set {fnt, sz, col} to {"HelveticaNeue", 15, {23387, 26471, 28784}}
				else if pStyle is "h2" then
					set {fnt, sz, col} to {"HelveticaNeue-Bold", 18, {2816, 15616, 11776}}
				else if pStyle is "h3" then
					set {fnt, sz, col} to {"HelveticaNeue-Bold", 15, {7710, 8995, 9766}}
				else if pStyle is "note" then
					set {fnt, sz, col} to {"HelveticaNeue-Italic", 13, {23387, 26471, 28784}}
				else if pStyle is "meta" then
					set {fnt, sz, col} to {"HelveticaNeue", 12, {35466, 38550, 40606}}
				else if pStyle is "space" then
					set {fnt, sz, col} to {"HelveticaNeue", 8, {7710, 8995, 9766}}
				end if
				set font of paragraph i of content of msg to fnt
				set size of paragraph i of content of msg to sz
				set color of paragraph i of content of msg to col
			end repeat
		end try
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
