import os
import re
from datetime import datetime, timedelta
import json
import requests
import pytz
import pandas as pd
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user
from dotenv import dotenv_values
from pathlib import Path
import secrets
import logging

# Determine the correct .env file path
env_path = Path('.env.local') if Path('.env.local').exists() else Path('.env')
print(f"Loading {env_path} file")

# Load environment variables into a dictionary
env_vars = dotenv_values(dotenv_path=env_path)

# Debug function to print environment variables
def print_env_vars():
    print("Environment variables after loading:")
    for key, value in env_vars.items():
        print(f"{key}: {value}")

print_env_vars()  # Print environment variables after loading

# Flask app setup
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(16))

# LinkedIn OAuth credentials
CLIENT_ID = env_vars.get('CLIENT_ID')
CLIENT_SECRET = env_vars.get('CLIENT_SECRET')
REDIRECT_URI = 'http://127.0.0.1:5000/login/authorized'
API_VERSION = env_vars.get('API_VERSION')
WEBHOOK_URL = env_vars.get('WEBHOOK_URL')
AUTHORIZATION_URL = 'https://www.linkedin.com/oauth/v2/authorization'
TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'

# Lead sync parameters
CMT_ACCOUNT_ID = env_vars.get('CMT_ACCOUNT_ID')
START_TIME = int((datetime.now() - timedelta(days=180)).timestamp() * 1000)
END_TIME = int(datetime.now().timestamp() * 1000)

# Login manager setup
login_manager = LoginManager()
login_manager.init_app(app)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, user_id):
        self.id = user_id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

@login_manager.unauthorized_handler
def unauthorized():
    return "Unauthorized!", 403

@app.route('/login')
def login():
    params = {
        'response_type': 'code',
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'state': secrets.token_urlsafe(16),  # Securely generated random state
        'scope': 'r_liteprofile,rw_ads,r_ads,r_emailaddress,r_marketing_leadgen_automation,r_organization_admin,r_events'  # Adjust scope based on your needs
    }
    url = requests.Request('GET', AUTHORIZATION_URL, params=params).prepare().url
    return redirect(url)

@app.route('/logout')
def logout():
    session.pop('linkedin_token', None)
    logout_user()
    return redirect(url_for('index'))

@app.route('/login/authorized')
def authorized():
    error = request.args.get('error', '')
    if error:
        return f"Error received: {error}", 400

    code = request.args.get('code')
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }
    response = requests.post(TOKEN_URL, data=data)
    response_data = response.json()

    if 'access_token' not in response_data:
        return "Failed to obtain access token.", 400

    session['linkedin_token'] = response_data['access_token']
    logger.info('Access token obtained successfully.')

    # Retrieve user profile data
    headers = {
        'Authorization': f"Bearer {session['linkedin_token']}",
        'cache-control': 'no-cache',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': API_VERSION
    }

    response = requests.get('https://api.linkedin.com/v2/me', headers=headers)
    profile_data = response.json()

    response = requests.get('https://api.linkedin.com/v2/emailAddress?q=members&projection=(elements*(handle~))', headers=headers)
    email_data = response.json()

    user_id = profile_data['id']
    first_name = profile_data['localizedFirstName']
    last_name = profile_data['localizedLastName']
    email = email_data['elements'][0]['handle~']['emailAddress']

    logger.info(f"{user_id}, {first_name} {last_name}, Logged in with email: {email}, access token: {session['linkedin_token']}")

    user = User(user_id)
    login_user(user)

    return redirect(url_for('sync_leads'))

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/chat', methods=['GET', 'POST'])
@login_required
def chat():
    # Protected route
    return "Protected route, restricted to logged-in users only!"

@app.route('/sync_leads')
@login_required
def sync_leads():
    lead_sync_api_url = f'https://api.linkedin.com/rest/leadFormResponses?q=owner&owner=(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{CMT_ACCOUNT_ID})&leadType=(leadType:SPONSORED)&limitedToTestLeads=false&submittedAtTimeRange=(start:{START_TIME},end:{END_TIME})&fields=ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,versionedLeadGenFormUrn,id,submittedAt,testLead,formResponse,form:(hiddenFields,creationLocale,name,id,content)&count=10&start=0'
    headers = {
        'Authorization': f"Bearer {session['linkedin_token']}",
        'cache-control': 'no-cache',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': API_VERSION
    }

    try:
        response = requests.get(lead_sync_api_url, headers=headers)
        response.raise_for_status()  # Raise an exception for HTTP errors

        leads_data = response.json().get('elements', [])
        all_extracted_data = []

        print(leads_data)
        for element in leads_data:
            response_id = element.get('id')
            form_id = extract_form_id(element.get('versionedLeadGenFormUrn'))
            form_response = element.get('formResponse', {})
            submitted_at = element.get('submittedAt')  # Get submittedAt timestamp
            answers = form_response.get('answers', [])

            # Extract account id, account name, campaign id, campaign name, and creative id
            account_id = element.get('owner', {}).get('sponsoredAccount', '').replace('urn:li:sponsoredAccount:', '')
            account_name = element.get('ownerInfo', {}).get('sponsoredAccountInfo', {}).get('name', '')
            campaign_id = element.get('leadMetadataInfo', {}).get('sponsoredLeadMetadataInfo', {}).get('campaign', {}).get('id', '').replace('urn:li:sponsoredCampaign:', '')
            campaign_name = element.get('leadMetadataInfo', {}).get('sponsoredLeadMetadataInfo', {}).get('campaign', {}).get('name', '')
            creative_id = element.get('associatedEntityInfo', {}).get('associatedCreative', {}).get('id', '').replace('urn:li:sponsoredCreative:', '')

            # Convert submittedAt timestamp to human-readable UTC date
            submitted_at_utc = convert_epoch_to_utc(submitted_at)

            # Fetch questions using get_form_questions function
            questions_info, form_name = get_form_questions(CMT_ACCOUNT_ID, form_id, session['linkedin_token'], API_VERSION)

            # Extract question answers
            extracted_data = extract_question_answer(answers, response_id, form_id, questions_info, form_name, submitted_at_utc)
            for data in extracted_data:
                data['account id'] = account_id
                data['account name'] = account_name
                data['campaign id'] = campaign_id
                data['campaign name'] = campaign_name
                data['creative id'] = creative_id

            all_extracted_data.extend(extracted_data)

        # Convert the extracted data to a pandas DataFrame
        df = pd.DataFrame(all_extracted_data)
        logging.info(f"Lead data synced and converted to DataFrame. Number of records: {len(df)}")

        # Post JSON payload to webhook URL if it exists and is not an empty string
        if WEBHOOK_URL:
            # Convert DataFrame to JSON
            json_payload = {"data": df.to_dict(orient='records')}
            post_to_webhook(WEBHOOK_URL, json_payload)
        else:
            logger.warning('WEBHOOK_URL is not defined or is an empty string. Skipping webhook posting.')

        return df.to_html()  # Render the DataFrame as an HTML table

    except requests.exceptions.RequestException as req_err:
        logger.error(f"Request error: {req_err}")
        if response is not None:
            logger.error(f"Response status code: {response.status_code}")
            logger.error(f"Response text: {response.text}")
        return jsonify({"error": "Request error", "message": str(req_err)}), 500

    except requests.exceptions.JSONDecodeError as json_err:
        logger.error(f"JSON decode error: {json_err}")
        logger.error(f"Response text: {response.text}")
        return jsonify({"error": "JSON decode error", "message": str(json_err), "response_text": response.text}), 500

def extract_form_id(versioned_lead_gen_form_urn):
    match = re.search(r':(\d+),', versioned_lead_gen_form_urn)
    return match.group(1) if match else None

def get_form_questions(sponsored_account_id, form_id, access_token, linkedin_version):
    url = f"https://api.linkedin.com/rest/leadForms?q=owner&owner=(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{sponsored_account_id})&count=9999&start=0"
    headers = {
        'LinkedIn-Version': linkedin_version,
        'X-Restli-Protocol-Version': '2.0.0',
        'Authorization': f'Bearer {access_token}'
    }

    response = requests.get(url, headers=headers)
    data = response.json() if response.status_code == 200 else {}

    for element in data.get('elements', []):
        if str(element.get('id')) == str(form_id):
            questions = element.get('content', {}).get('questions', [])
            questions_info = [(q['questionId'], q['question']['localized']['en_US']) for q in questions]
            return questions_info, element.get('name', 'Unknown Form')

    return [], 'Unknown Form'

def extract_question_answer(answers, response_id, form_id, questions_info, form_name, submitted_at_utc):
    question_dict = dict(questions_info)
    extracted_data = []

    for answer in answers:
        question_id = answer.get('questionId')
        question_label = question_dict.get(question_id, f'Question {question_id}')
        answer_text = answer.get('answerDetails', {}).get('textQuestionAnswer', {}).get('answer')
        extracted_data.append({
            'question': question_label,
            'answer': answer_text,
            'form name': form_name,
            'submittedAt': submitted_at_utc,
            'lead response id': response_id,
            'lead form id': form_id
        })

    return extracted_data

def convert_epoch_to_utc(epoch_ms):
    if epoch_ms:
        try:
            epoch_seconds = int(epoch_ms) / 1000
            utc_datetime = datetime.utcfromtimestamp(epoch_seconds).replace(tzinfo=pytz.UTC)
            return utc_datetime.strftime('%Y-%m-%d %H:%M:%S UTC')
        except Exception as e:
            logger.error(f"Error converting epoch to UTC: {e}")

    return None

def post_to_webhook(url, payload):
    headers = {
        'Content-Type': 'application/json',
        'cache-control': 'no-cache'
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logger.info('Payload successfully posted to webhook.')
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Request error: {req_err}")
        if response is not None:
            logger.error(f"Response status code: {response.status_code}")
            logger.error(f"Response text: {response.text}")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
