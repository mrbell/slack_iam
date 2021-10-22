'''
Slack commands for setting work status to notify your team when you're not in the office, e.g.
    /iam wfh
    /iam ooo tomorrow
    /iam ooo next week
'''

import boto3
from boto3.dynamodb.conditions import Key, Attr
import json
import logging
import os
import requests
from datetime import datetime, timedelta
import math
import parsedatetime as pdt
from pytz import timezone
from zappa.asynchronous import task
from traceback import format_exc
import time

from flask import abort, Flask, jsonify, request


"""
Resources
"""

help_text = "Use this command to set or check your status."
help_attachment_text = (
    "Use `/iam [subcommand]` with one of the following:\n"
    "\t -`wfh` to set a working from home status or `ooo` " +
    "to set out of office status followed by a time (defaults to today). \n" +
    "\t\t multiple dates can be given using 'and' or a range using 'through' \n" + 
    "\t\t and dates can be given in a range of natural formats, e.g. tomorrow, wednesday, 2019-03-12, etc.)\n"
    "\t -`in` to set your status to in office (to override an earlier OOO or WFH. \n" + 
    "\t -`history` to check your recent history, \n" +
    "\t -`today` to see everyone's status for the current day, \n" + 
    "\t -`schedule` to check scheduled OOO or WFH status. \n" + 
    "e.g. `/iam wfh tomorrow and friday`, `/iam ooo 4/13/2019`, or `/iam schedule`"
)

# Various tokens that we will need
logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamo = boto3.resource('dynamodb')
log_table_name = 'slack-iam-log'

webhook_url = os.environ['SLACK_WEBHOOK_URL']

app = Flask(__name__)

through_words = [' through ', ' to ', ' thru ']

class InvalidDate(Exception):
    pass


def is_request_valid(request):
    is_token_valid = request.form['token'] == os.environ['SLACK_VERIFICATION_TOKEN']
    is_team_id_valid = request.form['team_id'] == os.environ['SLACK_TEAM_ID']

    return is_token_valid and is_team_id_valid


def parse_subcommand(command_text):
    """
    Parse the subcommand from the given COMMAND_TEXT, which is everything that
    follows `/iam`.  The subcommand is the option passed to the command, e.g.
    'wfh' in the case of `/pickem wfh tomorrow`.
    """
    return command_text.strip().split()[0].lower()


def parse_options(command_text):
    """
    Parse options passed into the command, e.g. returns 'tomorrow' from the
    command `/iam wfh tomorrow`, where `iam` is the command, `wfh` is the
    subcommand, and `tomorrow` is the option passed to the subcommand.
    """
    sc = parse_subcommand(command_text)
    return command_text.replace(sc, '').strip()


def parse_date(date_str):
    cal = pdt.Calendar()
    
    eastern = timezone('US/Eastern')
    utc = timezone('UTC')
    source_time = utc.localize(datetime.utcnow()).astimezone(eastern)

    parsed_date_result = cal.parseDT(date_str, sourceTime=source_time)

    if parsed_date_result[1] > 0:
        parsed_date = parsed_date_result[0]
    else:
        parsed_date = None

    if parsed_date is None:
        raise InvalidDate(f'Could not parse the given date {date_str}')
    else:
        return str(parsed_date.date())


def submit_status(user_id, the_date, the_status, user_name=None):
    log_table = dynamo.Table(log_table_name)
    log_table.put_item(
        Item={
            'user_id': user_id,
            'date': the_date,
            'status': the_status,
            'user_name': user_name
        }
    ) 


def get_todays_status():
    log_table = dynamo.Table(log_table_name)
    response = log_table.query(
        IndexName='date-index',
        KeyConditionExpression=Key('date').eq(parse_date('today'))
    )
    all_statuses = response['Items']

    todays_statuses = sorted([
        f"{stat['user_name']} - {stat['status'].upper()}" 
        for stat in all_statuses 
        if stat['status'].lower() in ['wfh', 'ooo']
    ])

    return '\n'.join(todays_statuses)


def get_history(user_id):
    log_table = dynamo.Table(log_table_name)
    response = log_table.query(
        KeyConditionExpression=(
            Key('user_id').eq(user_id) &
            Key('date').gte(parse_date('a month ago')) 
        )
    )
    all_statuses = response['Items']

    past_statuses = sorted([
        f"{stat['date']} - {stat['status'].upper()}" 
        for stat in all_statuses 
        if stat['status'].lower() in ['wfh', 'ooo']
        and stat['date'] <= parse_date('today')
    ])

    return '\n'.join(past_statuses)


def parse_date_options(opts):

    if ' and ' in opts.lower():

        date_opts = opts.lower().split(' and ')

    elif any(tw in opts.lower() for tw in through_words):

        for tw in through_words:
            if tw in opts.lower():
                split_word = tw 
                break

        end_dates = opts.lower().split(split_word)

        assert len(end_dates) == 2, "Must provide a start and end date, e.g. 'monday through friday'"

        start_date, end_date = (
            datetime.strptime(parse_date(end_dates[0]), '%Y-%m-%d'), 
            datetime.strptime(parse_date(end_dates[1]), '%Y-%m-%d')
        )

        assert start_date <= end_date, "First date in range must come before the end date."

        dates = []
        
        while start_date <= end_date:
            dates.append(str(start_date.date()))
            start_date = start_date + timedelta(days=1)
        
        date_opts = dates

    else:
        date_opts = [opts]

    return [parse_date(date_opt) for date_opt in date_opts]


def get_schedule():
    log_table = dynamo.Table(log_table_name)
    response = log_table.scan()
    all_statuses = response['Items']

    start_date = parse_date('today')
    end_date = parse_date('a month from now')

    future_statuses = sorted([
        f"{stat['date']} - {stat['user_name']} - {stat['status'].upper()}" 
        for stat in all_statuses 
        if stat['status'].lower() in ['wfh', 'ooo']
        and stat['date'] >= start_date
        and stat['date'] <= end_date
    ])

    return '\n'.join(future_statuses)


@task
def log_time_task(response_url, subcommand, options, user_id, user_name):

    time.sleep(3)

    try:
        if len(options) == 0:
            options = 'today'
        
        the_dates = parse_date_options(options)

        for the_date in the_dates:
            submit_status(user_id, the_date, subcommand, user_name)

        if len(the_dates) == 1: 
        
            if the_date > parse_date('today'):
                response_text = f'{user_name} will be {subcommand.upper()} on {the_date}.'
            elif the_date == parse_date('today'):
                response_text = f'{user_name} is {subcommand.upper()} today.'
            else:
                response_text = f'{user_name} was {subcommand.upper()} on {the_date}.'

        elif ' and ' in options.lower():

            response_text = f'{user_name} is {subcommand.upper()} on '
            response_text += ' and '.join(the_dates)

        elif any(tw in options.lower() for tw in through_words):
            response_text = f'{user_name} is {subcommand.upper()} on {the_dates[0]} through {the_dates[-1]}'
        else:
            raise Exception("Something went wrong!")
    except:
        requests.post(response_url,
            json={
                'response_type': 'ephemeral',
                'text': 'Oops, something went wrong!',
                'attachments': [ 
                    dict(text=format_exc()), 
                ] 
            }
        )

    requests.post(response_url, 
        json={
            'response_type': 'in_channel',
            'text': response_text
        }
    )


@app.route('/iam', methods=['POST'])
def iam():
    if not is_request_valid(request):
        abort(400)

    request_text = request.form['text']

    subcommand = parse_subcommand(request_text)
    options = parse_options(request_text)

    user_id = request.form['user_id']
    user_name = request.form['user_name']

    if subcommand == 'wfh' or subcommand == 'ooo' or subcommand == 'in':

        # Use an async response just to prevent the command getting written to the channel for all to see
        log_time_task(request.form['response_url'], subcommand, options, user_id, user_name)

        return jsonify(
            response_type='ephemeral',
            text="logging..."
        )

    elif subcommand == 'help':
        return jsonify(
            text=help_text,
            attachments=[
                dict(text=help_attachment_text),
            ]
        )

    elif subcommand == 'schedule':
        try:
            future_statuses = get_schedule()
        except:
            return jsonify(
                response_type='ephemeral',
                text="Oops! Something went wrong!",
                attachments=[
                    dict(text=format_exc()),
                ]
            )
        
        return jsonify(
            response_type='in_channel',
            text="Upcoming WFH/OOO statuses:",
            attachments=[
                dict(text=future_statuses),
            ]
        ) 

    elif subcommand == 'today':
        try:
            todays_statuses = get_todays_status()
            if len(todays_statuses) == 0:
                todays_statuses = 'Everyone is planning to be in office today.'
        except:
            return jsonify(
                response_type='ephemeral',
                text="Oops! Something went wrong!",
                attachments=[
                    dict(text=format_exc()),
                ]
            )
        return jsonify(
            response_type='in_channel',
            text="Today's WFH/OOO statuses:",
            attachments=[
                dict(text=todays_statuses),
            ]
        )

    elif subcommand == 'history':
        try:
            past_statuses = get_history(user_id)
        except:
            return jsonify(
                response_type='ephemeral',
                text="Oops! Something went wrong!",
                attachments=[
                    dict(text=format_exc()),
                ]
            ) 
        return jsonify(
            text="My WFH/OOO status from the past month:",
            attachments=[
                dict(text=past_statuses),
            ]
        )

    else:
        return jsonify(
            text="Unknown subcommand!",
            attachments=[
                dict(text=help_attachment_text),
            ]
        )


def daily_update():

    todays_statuses = get_todays_status()
    if len(todays_statuses) == 0:
        todays_statuses = 'Everyone is planning to be in office today.'

    body = {
        'response_type': 'in_channel',
        'text': "Today's WFH/OOO statuses:"
    }

    body['attachments'] = [{'text': todays_statuses, 'mrkdwn_in': ['text']}]

    requests.post(
        webhook_url, 
        json=body,
        headers={'Content-Type': 'application/json'}
    )


if __name__ == '__main__':
    daily_update()
