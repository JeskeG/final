import hashlib

import flask
import requests
import sys
from flask import render_template
from flask import request
from flask import url_for
import uuid
import string
import random
from bson.objectid import ObjectId
import json
import logging

# Date handling 
import arrow # Replacement for datetime, based on moment.js
# import datetime # But we still need time
from dateutil import tz  # For interpreting local times


# OAuth2  - Google library implementation for convenience
from oauth2client import client
import httplib2   # used in oauth2 flow

# Google API for services 
from apiclient import discovery

###
# Globals
###
import config
from pymongo import MongoClient

if __name__ == "__main__":
    CONFIG = config.configuration()
else:
    CONFIG = config.configuration(proxied=True)

MONGO_CLIENT_URL = "mongodb://{}:{}@{}.{}/{}".format(
    CONFIG.DB_USER,
    CONFIG.DB_USER_PW,
    CONFIG.DB_HOST,
    CONFIG.DB_PORT,
    CONFIG.DB)

try:
    dbclient = MongoClient(MONGO_CLIENT_URL)
    db = getattr(dbclient, CONFIG.DB)
    collection = db.meetings

except:
    print("Failure opening database.  Is Mongo running? Correct password?")
    sys.exit(1)


print("Using URL '{}'".format(MONGO_CLIENT_URL))


app = flask.Flask(__name__)
app.debug = CONFIG.DEBUG
app.logger.setLevel(logging.DEBUG)
app.secret_key = CONFIG.SECRET_KEY

SCOPES = 'https://www.googleapis.com/auth/calendar.readonly'
CLIENT_SECRET_FILE = CONFIG.GOOGLE_KEY_FILE  ## You'll need this
APPLICATION_NAME = 'MeetMe class project'

#############################
#
#  Pages (routed from URLs)
#
#############################

@app.route("/")
@app.route("/index")
def index():
  app.logger.debug("Entering index")
  if 'begin_date' not in flask.session:
    init_session_values()
  return render_template('request.html')

@app.route("/choose")
def choose():
    return render_template('request.html')

@app.route("/login", methods=["POST"])
def login():
    app.logger.debug("Checking credentials for Google calendar access")
    credentials = valid_credentials()
    if not credentials:
        app.logger.debug("Redirecting to authorization")
        return flask.redirect(flask.url_for('oauth2callback'))

    gcal_service = get_gcal_service(credentials)
    app.logger.debug("Returned from get_gcal_service")
    flask.g.calendars = list_calendars(gcal_service)
    return flask.render_template('attendee.html')


@app.route("/respond/<meeting>/<ID>")
def respond(meeting, ID):
    flask.session['meeting'] = meeting
    flask.session['ID'] = ID
    db = collection.find_one({"_id": ObjectId(meeting)})
    flask.g.name = db["meeting_name"]
    beg = arrow.get(db['begin_date']).format("YYYY-MM-DD")
    end = arrow.get(db['end_date']).format("YYYY-MM-DD")
    flask.g.date = beg + " - " + end
    flask.g.time = db['begin_time'] + " - " + db['end_time']
    return flask.render_template("attendee.html")


@app.route("/schedule/<meet_id>")
def schedule(meet_id):
    meet_id = flask.session['meeting']
    return flask.render_template("schedule.html")


@app.route("/create_meeting", methods=["POST"])
def create_meeting():
    name = request.form["name"]
    emails = request.form.getlist("emails[]")
    app.logger.debug("meeting =" + name)
    app.logger.debug("emails = "+ str(emails))
    beg_time = flask.session['start_time']
    end_time = flask.session['stop_time']
    beg_date = flask.session['begin_date']
    end_date = flask.session['end_date']
    create_db(name, beg_date, end_date, beg_time, end_time, emails)
    app.logger.debug("DB created, redirecting to URLS")
    app.logger.debug("URL_list = " + str(flask.session['url_list']))
    return flask.jsonify(result=flask.url_for("add"))


@app.route("/add")
def add():
    flask.g.url_list = flask.session['url_list']
    return flask.render_template("add.html")


@app.route("/calculate")
def calculate():
    db = collection.find_one({"_id": ObjectId(flask.session['meeting'])})
    users = db['attendees']
    app.logger.debug("Starting to calculate free times")
    freetime = []
    busytime= []
    no_response=[]
    beg = arrow.get(db['begin_date']).format("YYYY-MM-DD")
    end = arrow.get(db['end_date']).format("YYYY-MM-DD")
    start_time = db['begin_time']
    end_time = db['end_time']
    start_hr = time_to_num(str(start_time))[0]
    start_min = time_to_num(str(start_time))[1]
    end_hr = time_to_num(str(end_time))[0]
    end_min = time_to_num(str(end_time))[1]
    date = arrow.get(beg)
    stop = arrow.get(end)
    app.logger.debug("Start hour to shift = " + str(start_hr))
    app.logger.debug("End hour to shift = " + str(end_hr))
    while date <= stop:
        s_date = date.shift(hours=start_hr, minutes=start_min)
        e_date = date.shift(hours=end_hr, minutes=end_min)
        freetime.append({"name": 'Free', "start": s_date, "end": e_date})
        date = date.shift(days=+1)
    for user in users:
        if not user['responded']:
            no_response.append(user['email'])
            continue
        if user['busy_times']:
            for event in user['busy_times']:
                busytime.append(event)
                app.logger.debug("A busy time = " + str(event))
    meet_time = calc_free_time(freetime, busytime)
    app.logger.debug("meet_times = " + str(meet_time))
    app.logger.debug("hasn't responded = " + str(no_response))
    meeting_info = {"meet_times": meet_time, "no_response": no_response}
    return flask.jsonify(result=meeting_info)


@app.route("/busy", methods=["POST"])
def busy():
    app.logger.debug("calculating busy times for {}".format(flask.session["ID"]))
    db = collection.find_one({"_id": ObjectId(flask.session["meeting"])})
    start_time = db['begin_time']
    end_time = db['end_time']
    start_hr = time_to_num(str(start_time))[0]
    start_min = time_to_num(str(start_time))[1]
    end_hr = time_to_num(str(end_time))[0]
    end_min = time_to_num(str(end_time))[1]
    beg = arrow.get(db['begin_date']).shift(hours=start_hr, minutes=start_min)
    app.logger.debug("Begin Time = " + str(beg))
    end = arrow.get(db['end_date']).shift(hours=end_hr, minutes=end_min)
    busy = calc_busy_time(beg, end)
    app.logger.debug("busy times = {}".format(str(busy)))
    update_busy_times(busy, flask.session['meeting'], flask.session["ID"])
    update_responded(flask.session['meeting'], flask.session["ID"])
    app.logger.debug("DB is updated")
    return flask.jsonify(result=flask.url_for("schedule", meet_id=flask.session['meeting']))


@app.route('/oauth2callback')
def oauth2callback():
  """
  The 'flow' has this one place to call back to.  We'll enter here
  more than once as steps in the flow are completed, and need to keep
  track of how far we've gotten. The first time we'll do the first
  step, the second time we'll skip the first step and do the second,
  and so on.
  """
  app.logger.debug("Entering oauth2callback")
  flow =  client.flow_from_clientsecrets(
      CLIENT_SECRET_FILE,
      scope= SCOPES,
      redirect_uri=flask.url_for('oauth2callback', _external=True))
  app.logger.debug("Got flow")
  if 'code' not in flask.request.args:
    app.logger.debug("Code not in flask.request.args")
    auth_uri = flow.step1_get_authorize_url()
    return flask.redirect(auth_uri)
  else:
    app.logger.debug("Code was in flask.request.args")
    auth_code = flask.request.args.get('code')
    credentials = flow.step2_exchange(auth_code)
    flask.session['credentials'] = credentials.to_json()
    app.logger.debug("Got credentials")
    return flask.redirect(flask.url_for('respond', meeting=flask.session['meeting'], ID=flask.session['ID']))


@app.route('/setrange', methods=['POST'])
def setrange():
    """
    User chose a date range with the bootstrap daterange
    widget.
    """
    app.logger.debug("Entering setrange")  
    flask.flash("Setrange gave us '{}'".format(
      request.form.get('daterange')))
    daterange = request.form.get('daterange')
    flask.session['start_time'] = request.form.get("begin_time")
    flask.session['stop_time'] = request.form.get('end_time')
    flask.session['daterange'] = daterange
    daterange_parts = daterange.split()
    flask.session['begin_date'] = interpret_date(daterange_parts[0])
    flask.session['end_date'] = interpret_date(daterange_parts[2])
    app.logger.debug("Setrange parsed {} - {}  dates as {} - {} and {} - {} times as {} - {}".format(
      daterange_parts[0], daterange_parts[1], 
      flask.session['begin_date'], flask.session['end_date'],
        flask.session['start_time'], flask.session['stop_time'],
        flask.session['begin_time'],flask.session['end_time']))
    return flask.redirect(flask.url_for("choose"))


#######
#FUNCTIONS USED


def update_busy_times(busy, meeting, user):
    collection.update_one({'_id': ObjectId(meeting), "attendees.ID": str(user)},
                          {"$set": {'attendees.$.busy_times': busy}}, upsert=True)


def update_responded(meeting, user):
    collection.update_one({'_id': ObjectId(meeting), "attendees.ID": str(user)},
                          {"$set": {"attendees.$.responded": True}}, upsert=True)


def calc_busy_time(beg, end):
    events = []
    cals = request.form.getlist('list[]')
    service = get_gcal_service(valid_credentials())
    for cal in cals:
        try:
            results = service.events().list(calendarId=cal, timeMin=beg, timeMax=end, singleEvents=True,
                                            orderBy="startTime").execute()
            real = results.get('items', [])
            for elem in real:
                if elem['start']['dateTime']:
                    events.append({"name": elem['summary'],
                                   "start": elem['start']['dateTime'],
                                   "end": elem['end']['dateTime']})
                else:  # all day event!
                    start = str(elem['start']['date'])+"T00:00:00-08:00"
                    end = str(elem['end']['date'])+"T24:00:00-08:00"
                    events.append({"name": elem['summary'], "start": start, "end": end})
        except:
            app.logger.debug("Something failed")
    app.logger.debug("events = " + str(events))
    return events


def calc_free_time(freetime, busytime):
    for free in freetime:
        for busy in busytime:
            free_start = arrow.get(free['start'])
            fs = free_start.time()
            free_end = arrow.get(free['end'])
            extra_free = free['end']
            fe = free_end.time()
            busy_start = arrow.get(busy['start'])
            bs = busy_start.time()
            busy_end = arrow.get(busy['end'])
            be = busy_end.time()
            if bs >= be:
                busytime.remove(busy)
                break
            if bs < fs:
                if be < fs:
                    busytime.remove(busy)
                    break
            if bs > fe:
                busytime.remove(busy)
                break
            if free_start.date() == busy_start.date():
                if free_start.date() == busy_end.date():  # single day event
                    if bs <= fs:
                        if be >= fe:  # busy throughout free
                            freetime.remove(free)
                            app.logger.debug("Free time completely removed")
                            break
                        else:
                            if be > fs:
                                free['start'] = busy['end']  # busy front overlaps
                                app.logger.debug("Free start = " + str(free_start) + " changed to " + str(busy['end']))
                                continue
                    if bs > fs:
                        if be < fe:
                            free['end'] = busy['start']  # busy cuts up free into two
                            app.logger.debug("Free end = " + str(free_end) + " changed to " + str(busy['start']))
                            freetime.append({"name": 'Free', "start": busy['end'],
                                             "end": extra_free})
                            app.logger.debug("New time created from "+str(busy['end'])+" to "+str(extra_free))
                            continue
                        elif be >= fe:
                            if bs < fe:
                                free['end'] = busy['start']  # busy back overlaps
                                app.logger.debug("Free end = " + str(free_end) + " changed to " + str(busy['start']))
                                continue
                elif busy_end.date() > free_end.date(): # multiday event
                    if bs <= fs:
                        freetime.remove(free)  # multiday event completely kills this free time
                        app.logger.debug("Free time completely removed")
                        break
                    if bs < fe:
                        free['end'] = busy['start']  # multiday event starts after free
                        app.logger.debug("Free end = " + str(free_end) + " changed to " + str(busy['start']))
                        continue
            elif free_start.date() == busy_end.date():
                if be > fs:  # wrap around from prev day busy event
                    if be < fe:
                        free["start"] = busy['end']
                        app.logger.debug("Free start = " + str(free_start) + " changed to " + str(busy['end']))
                        continue
                if be >= fe:
                        freetime.remove(free)
                        app.logger.debug("Free time completely removed")
                        break
    times = []
    for event in freetime:
        times.append({"event": event['name'], "start": arrow.get(event['start']).isoformat(),
                      "end": arrow.get(event['end']).isoformat()})
    times = sorted(times, key=lambda k: arrow.get(k['start']))
    return times


def valid_credentials():
    """
    Returns OAuth2 credentials if we have valid
    credentials in the session.  This is a 'truthy' value.
    Return None if we don't have credentials, or if they
    have expired or are otherwise invalid.  This is a 'falsy' value.
    """
    if 'credentials' not in flask.session:
      return None

    credentials = client.OAuth2Credentials.from_json(
        flask.session['credentials'])

    if (credentials.invalid or
        credentials.access_token_expired):
      return None
    return credentials


def get_gcal_service(credentials):
  """
  We need a Google calendar 'service' object to obtain
  list of calendars, busy times, etc.  This requires
  authorization. If authorization is already in effect,
  we'll just return with the authorization. Otherwise,
  control flow will be interrupted by authorization, and we'll
  end up redirected back to /choose *without a service object*.
  Then the second call will succeed without additional authorization.
  """
  app.logger.debug("Entering get_gcal_service")
  http_auth = credentials.authorize(httplib2.Http())
  service = discovery.build('calendar', 'v3', http=http_auth)
  app.logger.debug("Returning service")
  return service


def create_db(meet_name, beg_date, end_date, beg_time, end_time, attendees):
    attendee_list = []
    url_list = []
    for name in attendees:
        attendee_list.append({"email": name,
                              "responded": False,
                              "busy_times": None,
                              "ID": hashlib.md5(name.encode()).hexdigest()})
    meeting = {"meeting_name": meet_name,
               "begin_date": beg_date,
               "end_date": end_date,
               "begin_time": beg_time,
               "end_time": end_time,
               "attendees": attendee_list}
    flask.session['meet_id'] = str(collection.insert_one(meeting).inserted_id)
    for person in attendee_list:
        url_dict = {"name": person['email'],
                    "url": str(flask.url_for("respond",
                                             meeting=flask.session['meet_id'], ID=person["ID"], _external=True))}
        url_list.append(url_dict)
    flask.session['url_list'] = url_list
    return

def time_to_num(time_str):
    hh, mm = map(int, time_str.split(':'))
    return [hh, mm]



####
#
#   Initialize session variables 
#
####

def init_session_values():
    """
    Start with some reasonable defaults for date and time ranges.
    Note this must be run in app context ... can't call from main. 
    """
    # Default date span = tomorrow to 1 week from now
    now = arrow.now('local')     # We really should be using tz from browser
    tomorrow = now.replace(days=+1)
    nextweek = now.replace(days=+7)
    flask.session["begin_date"] = tomorrow.floor('day').isoformat()
    flask.session["end_date"] = nextweek.ceil('day').isoformat()
    flask.session["daterange"] = "{} - {}".format(
        tomorrow.format("MM/DD/YYYY"),
        nextweek.format("MM/DD/YYYY"))
    # Default time span each day, 8 to 5
    flask.session["begin_time"] = interpret_time("9am")
    flask.session["end_time"] = interpret_time("5pm")

def interpret_time( text ):
    """
    Read time in a human-compatible format and
    interpret as ISO format with local timezone.
    May throw exception if time can't be interpreted. In that
    case it will also flash a message explaining accepted formats.
    """
    app.logger.debug("Decoding time '{}'".format(text))
    time_formats = ["ha", "h:mma",  "h:mm a", "H:mm"]
    try: 
        as_arrow = arrow.get(text, time_formats).replace(tzinfo=tz.tzlocal())
        as_arrow = as_arrow.replace(year=2016) #HACK see below
        app.logger.debug("Succeeded interpreting time")
    except:
        app.logger.debug("Failed to interpret time")
        flask.flash("Time '{}' didn't match accepted formats 13:30 or 1:30pm"
              .format(text))
        raise
    return as_arrow.isoformat()

def interpret_date( text ):
    """
    Convert text of date to ISO format used internally,
    with the local time zone.
    """
    try:
      as_arrow = arrow.get(text, "MM/DD/YYYY").replace(
          tzinfo=tz.tzlocal())
    except:
        flask.flash("Date '{}' didn't fit expected format 12/31/2001")
        raise
    return as_arrow.isoformat()

def next_day(isotext):
    """
    ISO date + 1 day (used in query to Google calendar)
    """
    as_arrow = arrow.get(isotext)
    return as_arrow.replace(days=+1).isoformat()

####
#
#  Functions (NOT pages) that return some information
#
####
  
def list_calendars(service):
    """
    Given a google 'service' object, return a list of
    calendars.  Each calendar is represented by a dict.
    The returned list is sorted to have
    the primary calendar first, and selected (that is, displayed in
    Google Calendars web app) calendars before unselected calendars.
    """
    app.logger.debug("Entering list_calendars")  
    calendar_list = service.calendarList().list().execute()["items"]
    result = [ ]
    for cal in calendar_list:
        kind = cal["kind"]
        id = cal["id"]
        if "description" in cal: 
            desc = cal["description"]
        else:
            desc = "(no description)"
        summary = cal["summary"]
        # Optional binary attributes with False as default
        selected = ("selected" in cal) and cal["selected"]
        primary = ("primary" in cal) and cal["primary"]
        

        result.append(
          { "kind": kind,
            "id": id,
            "summary": summary,
            "selected": selected,
            "primary": primary
            })
    return sorted(result, key=cal_sort_key)


def cal_sort_key( cal ):
    """
    Sort key for the list of calendars:  primary calendar first,
    then other selected calendars, then unselected calendars.
    (" " sorts before "X", and tuples are compared piecewise)
    """
    if cal["selected"]:
       selected_key = " "
    else:
       selected_key = "X"
    if cal["primary"]:
       primary_key = " "
    else:
       primary_key = "X"
    return (primary_key, selected_key, cal["summary"])


#################
#
# Functions used within the templates
#
#################

@app.template_filter( 'fmtdate' )
def format_arrow_date( date ):
    try: 
        normal = arrow.get( date )
        return normal.format("ddd MM/DD/YYYY")
    except:
        return "(bad date)"

@app.template_filter( 'fmttime' )
def format_arrow_time( time ):
    try:
        normal = arrow.get( time )
        return normal.format("HH:mm")
    except:
        return "(bad time)"
    
#############


if __name__ == "__main__":
  # App is created above so that it will
  # exist whether this is 'main' or not
  # (e.g., if we are running under green unicorn)
  app.run(port=CONFIG.PORT,host="0.0.0.0")
    
