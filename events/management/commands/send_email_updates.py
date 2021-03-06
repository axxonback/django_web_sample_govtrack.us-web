from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from django.template import Context, Template
from django.template.loader import get_template
from django.conf import settings

from optparse import make_option

from events.models import *
from emailverification.models import Ping, BouncedEmail
from htmlemailer import send_mail as send_html_mail

import os, sys
from datetime import datetime, timedelta
import yaml, markdown2

now = datetime.now()

class Command(BaseCommand):
	args = 'daily|weekly|testadmin|testcount'
	help = 'Sends out email updates of events to subscribing users.'
	
	def handle(self, *args, **options):
		if len(args) != 1 or args[0] not in ('daily', 'weekly', 'testadmin', 'testcount'):
			print "Specify daily or weekly or testadmin or testcount."
			return

		if args[0] == "testadmin":
			# Test an email to the site administrator only. There's no need to be complicated
			# about finding users --- just use a hard-coded list.
			users = User.objects.filter(email="jt@occams.info")
			send_mail = True
			mark_lists = False
			send_old_events = True
			list_email_freq = (1,2)
		else:
			# Now that the number of registered users is much larger than the number of event-generating
			# feeds, it is faster to find users to update by starting with the most recent events.
			
			users = None
			send_mail = True
			mark_lists = True
			send_old_events = False
			if args[0] == "daily":
				list_email_freq = (1,)
				back_days = BACKFILL_DAYS_DAILY
			elif args[0] == "weekly":
				list_email_freq = (1,2)
				back_days = BACKFILL_DAYS_WEEKLY
			elif args[0] == "testcount":
				# count up how many daily emails we would send, but don't send any
				list_email_freq = (1,)
				send_mail = False
				mark_lists = False
				back_days = BACKFILL_DAYS_DAILY

			# Find the subscription lists w/ emails turned on.
			# Exclude lists we sent an email out to in the last 20 hours, in case we're
			# re-starting this process and some new events crept in.
			sublists = SubscriptionList.objects\
					.filter(email__in=list_email_freq)\
					.exclude(last_email_sent__gt=datetime.now()-timedelta(hours=20))

			# Find the feeds with new events first and then whittle down the list of
			# subscription lists to just those that track those feeds.
			if True:
				# Find all of the feeds tracked by all users with a subscription list with email updates turned on.
				all_feeds = list(Feed.objects.filter(tracked_in_lists__email__in = list_email_freq).distinct())

				# Feeds may be contained within other feeds. Make a map from feeds to sets of feeds they are contained
				# in. includes_feeds can be very dynamic, so we have to go forwards to make this map. We can't otherwise
				# go from a feed directly to feeds it is contained in.
				feed_included_in = dict()
				if sys.stdout.isatty():
					import random, tqdm
					random.shuffle(all_feeds)
					all_feeds = tqdm.tqdm(all_feeds, desc="Dynamic Feeds")
				for feed1 in all_feeds:
					feed1_includes = feed1.includes_feeds()
					for feed2 in feed1_includes:
						if feed2.id is None: raise Exception("Feed.includes_feeds() returned an object not in the database.")
						feed_included_in.setdefault(feed2.id, set()).add(feed1)
	
				# Find the feed IDs that generated all events in the last back_days days.
				if sys.stdout.isatty(): print "Looking for feeds with events..."
				active_feeds = set(Event.objects.filter(when__gt=datetime.now() - timedelta(days=back_days)).values_list("feed_id", flat=True))
	
				# Expand the active_feeds list to feeds that "included" any feeds in the active feeds list.
				# Feed inclusion isn't recursive beyond the first level so we can do this in one pass.
				for feed1 in list(active_feeds):
					for feed2 in feed_included_in.get(feed1, []):
						active_feeds.add(feed2)
	
				# Find the subscription lists w/ emails turned on that are tracking those feeds.
				# Exclude lists we sent an email out to in the last 16 hours, in case we're
				# re-starting this process and some new events crept in.
				sublists = sublists.filter(trackers__in=active_feeds)

			# Evaluate the query.
			if sys.stdout.isatty(): print "Looking for subscribed users..."
			sublists = set(sublists)

			# And get a list of those users.
			users = User.objects.filter(subscription_lists__in=sublists)\
				.distinct()\
				.only("id", "email", "last_login")\
				.order_by('id')

		if os.environ.get("START"):
			users = users.filter(id__gte=int(os.environ["START"]))
				#, id__lt=169660)
			
		total_emails_sent = 0
		total_events_sent = 0
		total_users_skipped_stale = 0
		total_users_skipped_bounced = 0

 		# get the list of user IDs to iterate through; clone up front to avoid holding the cursor (?)
		user_iterator = list(users)

		# when debugging, show a progress meter
		if sys.stdout.isatty(): 
			import tqdm
			user_iterator = tqdm.tqdm(user_iterator, desc="Users")

		# in degugging, clear the query log
		from django import db
		db.reset_queries()

		for user in user_iterator:
			# Check pingback status.
			try:
				p = Ping.objects.get(user=user)
			except Ping.DoesNotExist:
				p = None
				
			if user.last_login < datetime(2009, 4, 1) and p == None:
				# We warned these people on 2012-04-17 that if they didn't log in
				# they might stop getting email updates.
				total_users_skipped_stale += 1
				continue
			elif user.last_login < datetime.now() - timedelta(days=3) \
				and (not p or not p.pingtime or p.pingtime < datetime.now() - timedelta(days=20)) \
				and BouncedEmail.objects.filter(user=user).exists():
				total_users_skipped_bounced += 1
				continue
			
			events_sent = send_email_update(user, list_email_freq, send_mail, mark_lists, send_old_events)
			if events_sent != None:
				total_emails_sent += 1
				total_events_sent += events_sent
			
			# Write out the longest query.
			#print()
			#from django.db import connection
			#for q in sorted(connection.queries, reverse=True):
			#	print q["time"], q["sql"]
			from django import db
			db.reset_queries()
				
		print "Sent" if send_mail else "Would send", total_emails_sent, "emails and", total_events_sent, "events"
		print total_users_skipped_stale, "users skipped because they are stale"
		print total_users_skipped_bounced, "users skipped because of a bounced email"
			
def send_email_update(user, list_email_freq, send_mail, mark_lists, send_old_events):
	global now
	
	# get the email's From: header and return path
	emailfromaddr = getattr(settings, 'EMAIL_UPDATES_FROMADDR',
			getattr(settings, 'SERVER_EMAIL', 'no.reply@example.com'))
	emailreturnpath = emailfromaddr
	if hasattr(settings, 'EMAIL_UPDATES_RETURN_PATH'):
		emailreturnpath = (settings.EMAIL_UPDATES_RETURN_PATH % user.id)

	# Process each of the subscription lists.
	all_trackers = set()
	eventslists = []
	most_recent_event = None
	eventcount = 0
	for sublist in user.subscription_lists.all():
		# Get a list of all of the trackers this user has in all lists. We use the
		# complete list, even in non-email-update-lists, for rendering events.
		all_trackers |= set(sublist.trackers.all())

		# If this list does not have email updates turned on, move on.
		if sublist.email not in list_email_freq: continue

		# For debugging, clear the last_event_mailed flag so we can find some events
		# to send.
		if send_old_events: sublist.last_event_mailed = None

		# Get any new events to email the user about.
		max_id, events = sublist.get_new_events()
		if len(events) > 0:
			eventslists.append( (sublist, events) )
			eventcount += len(events)
			most_recent_event = max(most_recent_event, max_id)
	
	# Don't send an empty email.... less we're testing and we want to send some old events.
	if len(eventslists) == 0 and not send_old_events:
		return None
		
	if not send_mail:
		# don't email, don't update lists with the last emailed id
		return eventcount
	
	# Add a pingback image into the email to know (with some low accuracy) which
	# email addresses are still valid, for folks that have not logged in recently
	# and did not successfully recently ping back.
	emailpingurl = None
	if user.last_login < datetime.now() - timedelta(days=60) \
		and not Ping.objects.filter(user=user, pingtime__gt=datetime.now() - timedelta(days=60)).exists():
		emailpingurl = Ping.get_ping_url(user)
		
	# get announcement content
	announce = load_markdown_content("website/email/email_update_announcement.md")
		
	# send
	try:
		send_html_mail(
			"events/emailupdate",
			emailreturnpath,
			[user.email],
			{
				"user": user,
				"date": datetime.now().strftime("%b. %d").replace(" 0", " "),
				"eventslists": eventslists,
				"feed": all_trackers, # use all trackers in the user's account as context for displaying events
				"emailpingurl": emailpingurl,
				"SITE_ROOT_URL": settings.SITE_ROOT_URL,
				"announcement": announce
			},
			headers = {
				'From': emailfromaddr,
				'Auto-Submitted': 'auto-generated',
				'X-Auto-Response-Suppress': 'OOF',
			},
			fail_silently=False
		)
	except Exception as e:
		print user, e
		return None # skip updating what events were sent, False = did not sent
	
	if not mark_lists:
		return eventcount
	
	# mark each list as having mailed events up to the max id found from the
	# events table so that we know not to email those events in a future update.
	for sublist, events in eventslists:
		sublist.last_event_mailed = max(sublist.last_event_mailed, most_recent_event)
		sublist.last_email_sent = now
		sublist.save()
		
	return eventcount # did sent email

def load_markdown_content(template_path, utm=""):
	# Load the Markdown template for the current blast.
	templ = get_template(template_path)
	ctx = Context({ })

	# Get the text-only body content, which also includes some email metadata.
	# Replace Markdown-style [text][href] links with the text plus bracketed href.
	ctx.update({ "format": "text", "utm": "" })
	body_text = templ.render(ctx).strip()
	ctx.pop()
	body_text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 at \2", body_text)

	# The top of the text content contains metadata in YAML format,
	# with "id" and "subject" required.
	meta_info, body_text = body_text.split("----------", 1)
	meta_info = yaml.load(meta_info)
	body_text = body_text.strip()

	# Get the HTML body content.
	ctx.update({
		"format": "html",
		"utm": utm,
	})
	body_html = templ.render(ctx).strip()
	body_html = markdown2.markdown(body_html)
	ctx.pop()

	# Store everything in meta_info.
	
	meta_info["body_text"] = body_text
	meta_info["body_html"] = body_html
	
	return meta_info
