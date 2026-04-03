from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import mysql.connector
from mysql.connector import Error
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import os
import json
import threading
from werkzeug.utils import secure_filename
from datetime import date, datetime, time
from pathlib import Path
from functools import wraps
from flask_socketio import SocketIO, emit
from flask_mail import Mail, Message
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import smtplib
import socket
from dotenv import load_dotenv
from markupsafe import Markup, escape

load_dotenv()  # Load variables from .env file

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # Secure random secret key for sessions; store safely in prod
UPLOAD_FOLDER = 'static/uploads/profile_pics'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Flask-Mail configuration (replace with your actual email server details)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'  # e.g., 'smtp.gmail.com' for Gmail
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME') # Recommended to use environment variables
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD') # Recommended to use environment variables

# Twilio configuration for aSMS alerts
# Twilio configuration for SMS alerts
app.config['TWILIO_ACCOUNT_SID'] = os.environ.get('TWILIO_ACCOUNT_SID')
app.config['TWILIO_AUTH_TOKEN'] = os.environ.get('TWILIO_AUTH_TOKEN')
app.config['TWILIO_PHONE_NUMBER'] = os.environ.get('TWILIO_PHONE_NUMBER')

mail = Mail(app)

# Check if mail is configured to provide an early warning
if not all([app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD']]):
    print("WARNING: Mail credentials (MAIL_USERNAME, MAIL_PASSWORD) not fully configured. Email sending will fail.")

# Initialize Twilio Client
twilio_client = None
if all([app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'], app.config['TWILIO_PHONE_NUMBER']]):
    try:
        twilio_client = Client(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])
        print("Twilio client initialized successfully.")
    except Exception as e:
        print(f"Error initializing Twilio client: {e}")
else:
    print("Twilio credentials not fully configured. SMS sending will be disabled.")

socketio = SocketIO(app)
online_users = {} # maps user_id to sid

def get_db_connection():
    try:
        return mysql.connector.connect(
            host="localhost",
            user="root",
            password="@uttej123*",  
            database="blood_donation"  # Use your actual DB name
        )
    except Error as e:
        print("DB connection error:", e)
        return None

@app.context_processor
def inject_session():
    unread_count = 0
    store = globals().get('mock_store')
    if 'user_id' in session and store:
        unread_count = store.get_unread_count(session['user_id'])
    return dict(session=session, unread_count=unread_count, default_avatar='images/default-avatar.svg')

def calculate_age(born):
    if not born:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
app.jinja_env.globals.update(calculate_age=calculate_age)

@app.template_filter('nl2br')
def nl2br(value):
    return Markup('<br>'.join(escape((value or '')).splitlines()))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get-involved', methods=['GET', 'POST'])
def get_involved_page():
    if request.method == 'POST':
        form_type = request.form.get('form_type')
        conn = get_db_connection()
        if not conn:
            flash("Database connection failed.", "danger")
            return render_template('get-involved.html')

        cur = None  # Initialize cursor to None for the finally block
        try:

            if form_type == 'donor_update':
                if 'user_id' not in session:
                    return redirect(url_for('home'))

                user_id = session['user_id']
                name = request.form.get('name')
                dob = request.form.get('dob')
                gender = request.form.get('gender')
                contact = request.form.get('contact')
                pincode = request.form.get('pincode')
                blood_group = request.form.get('bloodgroup')

                cur = conn.cursor()
                cur.execute(
                    "UPDATE donor SET name=%s, dob=%s, gender=%s, contact=%s, pincode=%s, blood_group=%s WHERE profile_id=%s",
                    (name, dob, gender, contact, pincode, blood_group, user_id)
                )
                conn.commit()
                flash("Your donor profile has been updated successfully!", "success")
                return redirect(url_for('my_profile_page'))

            if form_type == 'signup':
                username = request.form.get('signup-name', '').strip()
                email = request.form.get('signup-email', '').strip().lower()
                password = request.form.get('signup-password', '')
                confirm = request.form.get('signup-confirm-password', '')
                dob = request.form.get('signup-dob')
                gender = request.form.get('signup-gender')
                contact = request.form.get('signup-contact')
                pincode = request.form.get('signup-pincode', '').strip()
                blood_group = request.form.get('signup-bloodgroup')

                if not all([username, email, password, dob, gender, contact, pincode, blood_group]):
                    flash("Please fill all signup fields.", "warning")
                    return render_template('get-involved.html')
                if password != confirm:
                    flash("Passwords do not match.", "warning")
                    return render_template('get-involved.html')

                cur = conn.cursor()
                cur.execute("SELECT user_id FROM profile WHERE username=%s OR email=%s", (username, email))
                if cur.fetchone():
                    flash("Username or email already exists. Please choose different ones.", "warning")
                    cur.close()
                    return render_template('get-involved.html')

                hashed = generate_password_hash(password)

                cur.execute("INSERT INTO profile (username, email, password) VALUES (%s, %s, %s)", (username, email, hashed))
                profile_id = cur.lastrowid

                cur.execute(
                    "INSERT INTO donor (profile_id, name, dob, gender, contact, pincode, blood_group) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (profile_id, username, dob, gender, contact, pincode, blood_group)
                )

                conn.commit()
                flash("Signup successful! Please log in.", "success")
                return redirect(url_for('get_involved_page'))

            elif form_type == 'login':
                identifier = request.form.get('login-email', '').strip()
                password = request.form.get('login-password', '')
                if not identifier or not password:
                    flash("Please enter your credentials.", "warning")
                    return render_template('get-involved.html')

                lower_identifier = identifier.lower()
                cur = conn.cursor(dictionary=True)
                cur.execute("""
                    SELECT p.user_id, p.username, p.password, d.profile_picture_url
                    FROM profile p LEFT JOIN donor d ON p.user_id = d.profile_id
                    WHERE p.username=%s OR p.email=%s
                """, (identifier, lower_identifier))
                user = cur.fetchone()

                if user and check_password_hash(user['password'], password):
                    session['user_id'] = user['user_id']
                    session['username'] = user['username']
                    session['profile_pic_url'] = user.get('profile_picture_url')
                    flash(f"Welcome back, {user['username']}!", "success")
                    return redirect(url_for('my_profile_page'))
                else:
                    flash("Invalid credentials. Please try again.", "danger")
                    return render_template('get-involved.html')

        except Error as e:
            if conn:
                conn.rollback()  # Roll back the transaction on any DB error
            print(f"DB error on {form_type}:", e)
            flash("A database error occurred.", "danger")
        finally:
            if cur:
                cur.close()
            if conn and conn.is_connected():
                conn.close()

    # GET request logic
    donor_data = None
    if 'user_id' in session:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor(dictionary=True)
                cur.execute("SELECT * FROM donor WHERE profile_id = %s", (session['user_id'],))
                donor_data = cur.fetchone()
            except Error as e:
                print("Error fetching donor data for form:", e)
            finally:
                if conn and conn.is_connected():
                    cur.close()
                    conn.close()

    return render_template('get-involved.html', donor_data=donor_data)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/my-profile', methods=['GET', 'POST'])
def my_profile_page():
    if 'user_id' not in session:
        flash("Please log in to view this page.", "warning")
        return redirect(url_for('get_involved_page'))

    user_id = session['user_id']
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return render_template('my-profile.html', user=None)

    if request.method == 'POST':
        fullname = request.form.get('fullName')
        email = request.form.get('email')
        phone = request.form.get('phone')
        pincode = request.form.get('pincode')
        dob = request.form.get('dob')
        blood_group = request.form.get('bloodgroup')
        profile_pic = request.files.get('profile-picture')

        try:
            cur = conn.cursor()
            
            pfp_url = None
            if profile_pic and allowed_file(profile_pic.filename):
                filename = secure_filename(f"user_{user_id}_{profile_pic.filename}")
                # Ensure the upload directory exists
                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                profile_pic.save(filepath)
                pfp_url = f"uploads/profile_pics/{filename}"

                # Update profile picture URL in the database
                cur.execute("UPDATE donor SET profile_picture_url=%s WHERE profile_id=%s", (pfp_url, user_id))
                session['profile_pic_url'] = pfp_url # Update session immediately

            cur.execute("UPDATE profile SET username=%s, email=%s WHERE user_id=%s", (fullname, email, user_id))
            cur.execute(
                "UPDATE donor SET name=%s, contact=%s, pincode=%s, dob=%s, blood_group=%s WHERE profile_id=%s",
                (fullname, phone, pincode, dob, blood_group, user_id)
            )
            session['username'] = fullname
            
            conn.commit()
            cur.close()
            flash("Profile updated successfully!", "success")
        except Error as e:
            print("DB error on profile update:", e)
            flash("An error occurred while updating your profile.", "danger")
        finally:
            if conn.is_connected():
                conn.close()
        return redirect(url_for('my_profile_page'))

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.username, p.email, d.name, d.dob, d.gender, d.blood_group, d.pincode, d.contact, d.profile_picture_url
            FROM profile p LEFT JOIN donor d ON p.user_id = d.profile_id WHERE p.user_id = %s
        """, (user_id,))
        user_data = cur.fetchone()

        # Fetch donation history
        cur.execute("""
            SELECT dn.donation_date, dn.location, dn.units
            FROM donations dn
            JOIN donor d ON dn.donor_id = d.donor_id
            WHERE d.profile_id = %s
            ORDER BY dn.donation_date DESC
        """, (user_id,))
        donation_history = cur.fetchall()
        cur.close()
    except Error as e:
        print("DB error fetching profile:", e)
        flash("Could not load your profile.", "danger")
        user_data = None
        donation_history = []
    finally:
        if conn.is_connected():
            conn.close()

    return render_template('my-profile.html', user=user_data, donation_history=donation_history)


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

@app.route('/search-donors', methods=['GET', 'POST'])
def search_donors_page():
    donors = []
    if request.method == 'POST':
        blood_group = request.form.get('bloodgroup')
        pincode = request.form.get('pincode', '').strip()
        conn = get_db_connection()
        if not conn:
            flash("Database connection failed.", "danger")
            return render_template('search-donors.html', donors=[])
        
        try:
            cur = conn.cursor(dictionary=True)
            # Join with profile table to get email for the request button
            query = """
                SELECT d.name, d.dob, d.gender, d.blood_group, d.pincode, d.contact, p.email, p.user_id
                FROM donor d
                JOIN profile p ON d.profile_id = p.user_id
                WHERE d.blood_group = %s AND d.pincode = %s
            """
            cur.execute(query, (blood_group, pincode))
            donors = cur.fetchall()
            cur.close()
            if not donors:
                flash(f"No donors found for blood group {blood_group} in PIN code {pincode}.", "info")
        except Error as e:
            print("DB error on donor search:", e)
            flash("An error occurred during the search.", "danger")
        finally:
            if conn.is_connected():
                conn.close()

    return render_template('search-donors.html', donors=donors)

@app.route('/leaderboard')
def leaderboard_page():
    leaders = []
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return render_template('leaderboard.html', leaders=[])
    
    try:
        cur = conn.cursor(dictionary=True)
        # Real query to calculate top donors from the donations table
        query = """
            SELECT d.name, d.pincode, COUNT(dn.donation_id) as donations
            FROM donations dn
            JOIN donor d ON dn.donor_id = d.donor_id
            GROUP BY d.donor_id, d.name, d.pincode
            ORDER BY donations DESC
            LIMIT 10
        """
        cur.execute(query)
        leaders = cur.fetchall()
        
        # Add mock donation counts and prizes
        for i, leader in enumerate(leaders):
            leader['prize'] = f"${max(0, 300 - i*100)}" if i < 3 else "---"
    except Error as e:
        print("DB error on leaderboard fetch:", e)
        flash("Could not load the leaderboard.", "danger")
    finally:
        if conn and conn.is_connected():
            cur.close()
            conn.close()

    return render_template('leaderboard.html', leaders=leaders)

@app.route('/blood-request', methods=['GET', 'POST'])
def blood_request_page():
    if 'user_id' not in session:
        flash("Please log in to make a blood request.", "warning")
        return redirect(url_for('get_involved_page'))

    if request.method == 'POST':
        requester_id = session['user_id']
        blood_group = request.form.get('bloodgroup')
        pincode = request.form.get('pincode')
        
        # Find donors matching the criteria, excluding the requester
        donors = get_donors_for_request(blood_group, pincode, exclude_user_id=requester_id)

        if not donors:
            flash(f"No donors found for blood group {blood_group} in PIN code {pincode}. Your request was not sent.", "info")
            return redirect(url_for('blood_request_page'))

        # Construct the message from form data
        subject = f"Blood Request for {blood_group}"
        body = f"""
        A request has been made for your blood group in your area.
        Patient Name: {request.form.get('patient-name')}
        Required Units: {request.form.get('units')}
        Hospital: {request.form.get('hospital')}
        Contact Person: {request.form.get('contact-person')}
        Contact Phone: {request.form.get('contact-phone')}
        Reason: {request.form.get('reason')}
        """

        conn = get_db_connection()
        if not conn:
            flash("Database connection failed. Request could not be sent.", "danger")
            return render_template('blood-request.html')

        sent_count = 0
        try:
            cur = conn.cursor()
            for donor in donors:
                donor_id = donor['user_id']
                cur.execute(
                    "INSERT INTO messages (sender_id, receiver_id, subject, body) VALUES (%s, %s, %s, %s)",
                    (requester_id, donor_id, subject, body)
                )
                sent_count += 1
            
            conn.commit()
            flash(f"Your request has been sent as an internal message to {sent_count} matching donor(s)!", "success")
        except Error as e:
            print("DB error sending bulk message:", e)
            flash("An error occurred while sending your request to donors.", "danger")
        finally:
            if conn.is_connected():
                cur.close()
                conn.close()

        return redirect(url_for('home'))
    return render_template('blood-request.html')

@app.route('/blood-drives')
def blood_drives_page():
    drives = []
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return render_template('blood-drives.html', drives=[])
    
    try:
        cur = conn.cursor(dictionary=True)
        # Fetch drives that are in the future
        query = "SELECT * FROM blood_drives WHERE drive_date >= CURDATE() ORDER BY drive_date ASC"
        cur.execute(query)
        drives = cur.fetchall()
    except Error as e:
        print("DB error on blood drives fetch:", e)
        flash("Could not load blood drives.", "danger")
    finally:
        if conn and conn.is_connected():
            cur.close()
            conn.close()

    return render_template('blood-drives.html', drives=drives)

# --- Placeholder Routes to prevent 404 errors ---

@app.route('/faqs')
def faqs_page():
    return render_template('faqs.html')

@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login_page():
    if request.method == 'POST':
        flash("Admin login functionality is not yet implemented.", "info")
    return render_template('admin-login.html')

# ... <everything above remains unchanged> ...

@app.route('/emergency-request', methods=['GET', 'POST'])
def emergency_request_page():
    if 'user_id' not in session:
        flash("You must be logged in to send an emergency alert.", "warning")
        return redirect(url_for('get_involved_page'))

    if request.method == 'POST':
        blood_type = request.form.get('bloodgroup')
        pincode = request.form.get('pincode')
        contact_phone = request.form.get('contact-phone')
        user_id = session['user_id']

        if not all([blood_type, pincode, contact_phone]):
            flash("Blood group, PIN code, and a contact phone are required for an emergency alert.", "danger")
            return redirect(url_for('emergency_request_page'))

        # Log the emergency alert to the database for auditing
        db_conn = get_db_connection()
        if not db_conn:
            flash("Database connection failed. Could not log the alert.", "danger")
            # The alert will still proceed even if logging fails.
        else:
            try:
                cur = db_conn.cursor()
                cur.execute(
                    "INSERT INTO emergency_alerts (triggered_by_user_id, blood_group_needed, pincode, contact_phone) VALUES (%s, %s, %s, %s)",
                    (user_id, blood_type, pincode, contact_phone)
                )
                db_conn.commit()
            except Error as e:
                print(f"Error logging emergency alert: {e}")
                flash("An error occurred while logging the emergency alert, but the alert will still be sent.", "warning")
            finally:
                if db_conn.is_connected():
                    cur.close()
                    db_conn.close()
                    
        # Emit a real-time alert to online users
        socketio.emit('emergency_alert', {'blood_type': blood_type, 'pincode': pincode})

        # Find recipients by blood type AND pincode
        recipients = get_users_by_blood_type(blood_type, pincode)
        emergency_base = url_for("donor_response", _external=True)

        email_sent_count, sms_sent_count = 0, 0
        email_fail_count, sms_fail_count = 0, 0

        if recipients:
            for email, phone_number in recipients:
                # --- Email Logic with Flask-Mail ---
                try:
                    response_link = f"{emergency_base}?email={email}&blood_type={blood_type}"
                    msg = Message(subject=f"🚨 Emergency Blood Request: {blood_type} Needed",
                                  sender=app.config['MAIL_USERNAME'],
                                  recipients=[email])
                    msg.html = f"""<h2>Urgent need for {blood_type} blood!</h2><p>As a <b>{blood_type}</b> donor in your area, your help could save a life. Please respond immediately!</p><a href="{response_link}" style="background:#e63946;color:#fff;padding:12px 24px;text-decoration:none;font-weight:bold;border-radius:6px;display:inline-block;">🚑 Respond Now</a>"""
                    mail.send(msg)
                    email_sent_count += 1
                except Exception as e:
                    print(f"Mail Error sending to {email}: {e}")
                    email_fail_count += 1

                # --- SMS Logic with Twilio ---
                if twilio_client and phone_number:
                    normalized_phone = normalize_phone_number(phone_number)
                    if not normalized_phone:
                        print(f"Skipping SMS to invalid or unformatted phone number: {phone_number}")
                        sms_fail_count += 1
                        continue
                    try:
                        message_body = f"URGENT Blood Request from Oasis: Type {blood_type} needed near PIN {pincode}. If you can help, please contact {contact_phone} immediately."
                        twilio_client.messages.create(body=message_body, from_=app.config['TWILIO_PHONE_NUMBER'], to=normalized_phone)
                        sms_sent_count += 1
                    except TwilioRestException as tw_e:
                        print(f"Twilio Error sending SMS to {normalized_phone}: {tw_e}")
                        sms_fail_count += 1
                    except Exception as e:
                        print(f"General error sending SMS to {normalized_phone}: {e}")
                        sms_fail_count += 1

            flash_message = f"Emergency alert for {blood_type} sent to matching donors. Emails: {email_sent_count} sent"
            if email_fail_count > 0: flash_message += f", {email_fail_count} failed."
            else: flash_message += "."
            if sms_sent_count > 0 or sms_fail_count > 0:
                flash_message += f" SMS: {sms_sent_count} sent"
                if sms_fail_count > 0: flash_message += f", {sms_fail_count} failed."
                else: flash_message += "."
            flash(flash_message, "success" if email_fail_count == 0 and sms_fail_count == 0 else "warning")

        else:
            # Fallback: if no specific donors are found, alert everyone
            all_users = get_all_users()
            if not all_users:
                flash("❌ No registered donors in the system to alert.", "danger")
                return redirect(url_for('home'))

            for email, donor_type, phone_number in all_users:
                try:
                    response_link = f"{emergency_base}?email={email}&blood_type={blood_type}"
                    msg = Message(subject=f"🚨 Emergency Blood Request: {blood_type} Needed",
                                  sender=app.config['MAIL_USERNAME'],
                                  recipients=[email])
                    msg.html = f"""<h2>Urgent need for {blood_type} blood!</h2><p>You are registered as <b>{donor_type}</b>. Even if not a perfect match, please check if you can donate or help connect someone who can.</p><a href="{response_link}" style="background:#e63946;color:#fff;padding:12px 24px;text-decoration:none;font-weight:bold;border-radius:6px;display:inline-block;">🚑 Respond Now</a>"""
                    mail.send(msg)
                    email_sent_count += 1
                except Exception as e:
                    print(f"Mail Error sending to {email}: {e}")
                    email_fail_count += 1
                
                if twilio_client and phone_number:
                    normalized_phone = normalize_phone_number(phone_number)
                    if normalized_phone:
                        try:
                            message_body = f"URGENT Blood Request from Oasis: Type {blood_type} needed near PIN {pincode}. A specific match was not found, but your help may be needed. Contact {contact_phone}."
                            twilio_client.messages.create(body=message_body, from_=app.config['TWILIO_PHONE_NUMBER'], to=normalized_phone)
                            sms_sent_count += 1
                        except TwilioRestException as tw_e:
                            print(f"Twilio Fallback Error sending SMS to {normalized_phone}: {tw_e}")
                            sms_fail_count += 1
                        except Exception as e:
                            print(f"General error sending SMS to {normalized_phone}: {e}")
                            sms_fail_count += 1
            
            flash_message = f"⚠ No {blood_type} donors found in {pincode}. Alert sent to ALL {len(all_users)} donors as a fallback. Emails: {email_sent_count} sent"
            if email_fail_count > 0: flash_message += f", {email_fail_count} failed."
            else: flash_message += "."
            if sms_sent_count > 0 or sms_fail_count > 0:
                flash_message += f" SMS: {sms_sent_count} sent"
                if sms_fail_count > 0: flash_message += f", {sms_fail_count} failed."
                else: flash_message += "."
            flash(flash_message, "warning")

        return redirect(url_for('home'))
    return render_template('emergency-request.html')

@app.route('/request-donor/<int:donor_id>', methods=['GET', 'POST'])
def request_donor_page(donor_id):
    if 'user_id' not in session:
        flash("You must be logged in to make a request.", "warning")
        return redirect(url_for('get_involved_page'))

    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return redirect(url_for('search_donors_page'))

    donor = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT p.user_id, d.name, d.blood_group FROM donor d JOIN profile p ON d.profile_id = p.user_id WHERE p.user_id = %s", (donor_id,))
        donor = cur.fetchone()
    except Error as e:
        print("DB error fetching donor for request page:", e)
    finally:
        if conn and conn.is_connected():
            cur.close()
            conn.close()

    if not donor:
        flash("Donor not found.", "danger")
        return redirect(url_for('search_donors_page'))

    if request.method == 'POST':
        requester_id = session['user_id']
        subject = f"Blood Request for {request.form.get('blood_group')}"
        body = f"""
        Patient Name: {request.form.get('patient_name')}
        Required Units: {request.form.get('units')}
        Hospital: {request.form.get('hospital')}
        Contact: {request.form.get('contact_phone')}
        Reason: {request.form.get('reason')}
        """
        conn = get_db_connection() # Re-open connection for POST
        if not conn:
            flash("Database connection failed.", "danger")
            return render_template('request-donor.html', donor=donor)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (sender_id, receiver_id, subject, body) VALUES (%s, %s, %s, %s)",
                (requester_id, donor_id, subject, body)
            )
            conn.commit()
            cur.close()

            # Emit real-time event to the receiver if they are online
            receiver_sid = online_users.get(donor_id)
            if receiver_sid:
                emit('new_message', {
                    'sender_id': requester_id,
                    'sender_username': session.get('username'),
                    'body': body,
                    'created_at': date.today().strftime('%b %d, %I:%M %p') # Simplified time for real-time
                }, to=receiver_sid)

            flash("Your request has been sent to the donor!", "success")
            return redirect(url_for('conversation_page', other_user_id=donor_id))
        except Error as e:
            print("DB error sending message:", e)
            flash("Could not send your request.", "danger")
        finally:
            if conn and conn.is_connected():
                conn.close()

    return render_template('request-donor.html', donor=donor)

@app.route('/inbox')
def inbox_page():
    if 'user_id' not in session:
        flash("Please log in to view your inbox.", "warning")
        return redirect(url_for('get_involved_page'))
    
    user_id = session['user_id']
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return render_template('inbox.html', conversations=[])

    conversations = []
    try:
        # Corrected query to get the last message from each conversation partner, including profile picture
        query = """
            SELECT p.user_id, p.username, d.profile_picture_url, m.body, m.created_at, m.is_read,
                   IF(m.sender_id = %s, 0, 1) as is_other_sender
            FROM messages m
            JOIN profile p ON p.user_id = IF(m.sender_id = %s, m.receiver_id, m.sender_id)
            LEFT JOIN donor d ON p.user_id = d.profile_id
            WHERE m.message_id IN (
                SELECT MAX(message_id) FROM messages WHERE sender_id = %s OR receiver_id = %s
                GROUP BY LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id)
            ) ORDER BY m.created_at DESC
        """
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (user_id, user_id, user_id, user_id))
        conversations = cur.fetchall()
    except Error as e:
        print("DB error on inbox fetch:", e)
        flash("Could not load your inbox.", "danger")
    finally:
        if conn and conn.is_connected():
            cur.close()
            conn.close()
    return render_template('inbox.html', conversations=conversations)

@app.route('/conversation/<int:other_user_id>', methods=['GET', 'POST'])
def conversation_page(other_user_id):
    if 'user_id' not in session:
        return redirect(url_for('get_involved_page'))

    user_id = session['user_id']

    if request.method == 'POST':
        body = request.form.get('body')
        if body:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO messages (sender_id, receiver_id, subject, body) VALUES (%s, %s, %s, %s)", (user_id, other_user_id, 'Re: Blood Request', body))
                    conn.commit()

                    # Emit real-time event to the receiver if they are online
                    receiver_sid = online_users.get(other_user_id)
                    if receiver_sid:
                        emit('new_message', {
                            'sender_id': user_id,
                            'sender_username': session.get('username'),
                            'body': body,
                            'created_at': date.today().strftime('%b %d, %I:%M %p') # Simplified time
                        }, to=receiver_sid)
                except Error as e:
                    print("DB error sending reply:", e)
                    flash("Could not send your reply.", "danger")
                finally:
                    if conn and conn.is_connected():
                        cur.close()
                        conn.close()
        return redirect(url_for('conversation_page', other_user_id=other_user_id))

    # GET request logic
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "danger")
        return redirect(url_for('inbox_page'))
        
    messages = []
    other_user = None
    cur = None
    try:
        cur = conn.cursor(dictionary=True)

        # Mark all messages from other_user as read
        cur.execute("UPDATE messages SET is_read = 1 WHERE receiver_id = %s AND sender_id = %s", (user_id, other_user_id))
        conn.commit()

        # Fetch conversation messages
        cur.execute("SELECT m.*, p.username as sender_username FROM messages m JOIN profile p ON m.sender_id = p.user_id WHERE (m.sender_id = %s AND m.receiver_id = %s) OR (m.sender_id = %s AND m.receiver_id = %s) ORDER BY m.created_at ASC", (user_id, other_user_id, other_user_id, user_id))
        messages = cur.fetchall()

        # Fetch other user's info
        cur.execute("SELECT username FROM profile WHERE user_id = %s", (other_user_id,))
        other_user = cur.fetchone()
    except Error as e:
        print("DB error fetching conversation:", e)
        flash("Could not load conversation.", "danger")
    finally:
        if cur:
            cur.close()
        if conn and conn.is_connected():
            conn.close()
    return render_template('conversation.html', messages=messages, other_user=other_user, other_user_id=other_user_id)

@socketio.on('connect')
def handle_connect():
    user_id = session.get('user_id')
    if user_id:
        online_users[user_id] = request.sid
        print(f"User {user_id} connected with sid {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    # Find which user disconnected and remove them from the online list
    for user_id, sid in list(online_users.items()):
        if sid == request.sid:
            del online_users[user_id]
            print(f"User {user_id} disconnected.")
            break

def normalize_phone_number(phone_number, default_country_code='+91'):
    """
    A simple function to normalize a phone number to E.164 format for Twilio.
    Assumes Indian numbers if no country code is present.
    """
    if not phone_number:
        return None
    
    # If the number already has a country code, just clean it
    if phone_number.strip().startswith('+'):
        return '+' + ''.join(filter(str.isdigit, phone_number))

    # Remove all non-digit characters
    cleaned_number = ''.join(filter(str.isdigit, phone_number))

    # If it's a 10-digit number, assume it's a local Indian number
    if len(cleaned_number) == 10:
        return f"{default_country_code}{cleaned_number}"
    
    # If it's 12 digits and starts with 91, it's probably an Indian number with code but no +
    if len(cleaned_number) == 12 and cleaned_number.startswith('91'):
        return f"+{cleaned_number}"

    # Return None if format is not recognized, to prevent sending invalid requests
    return None

def get_users_by_blood_type(blood_type, pincode):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=False) # Return tuples
        # Query to get emails of donors with a specific blood group and pincode
        query = """
            SELECT p.email, d.contact
            FROM profile p
            JOIN donor d ON p.user_id = d.profile_id
            WHERE d.blood_group = %s AND d.pincode = %s
        """
        cur.execute(query, (blood_type, pincode))
        recipients = cur.fetchall()
        return recipients
    except Error as e:
        print(f"Error fetching users by blood type: {e}")
        return []
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()

def get_all_users():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=False) # Return tuples
        # Query to get email and blood group for all donors
        query = """
            SELECT p.email, d.blood_group, d.contact
            FROM profile p
            JOIN donor d ON p.user_id = d.profile_id
        """
        cur.execute(query)
        users = cur.fetchall()
        return users
    except Error as e:
        print(f"Error fetching all users: {e}")
        return []
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()

def get_donors_for_request(blood_type, pincode, exclude_user_id=None):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        query = """
            SELECT p.user_id
            FROM profile p
            JOIN donor d ON p.user_id = d.profile_id
            WHERE d.blood_group = %s AND d.pincode = %s
        """
        params = [blood_type, pincode]
        if exclude_user_id:
            query += " AND p.user_id != %s"
            params.append(exclude_user_id)
            
        cur.execute(query, tuple(params))
        recipients = cur.fetchall()
        return recipients
    except Error as e:
        print(f"Error fetching donors for request: {e}")
        return []
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()

@app.route('/donor-response')
def donor_response():
    email = request.args.get('email')
    blood_type = request.args.get('blood_type')

    if not email or not blood_type:
        flash("Invalid response link.", "danger")
        return redirect(url_for('home'))

    conn = get_db_connection()
    if not conn:
        flash("Database connection error. Could not log your response.", "danger")
        return redirect(url_for('home'))

    try:
        cur = conn.cursor(dictionary=True)
        # Find user_id from email
        cur.execute("SELECT user_id FROM profile WHERE email = %s", (email,))
        user = cur.fetchone()

        if user:
            user_id = user['user_id']
            # Insert into responses table
            cur.execute("INSERT INTO responses (user_id, blood_type_needed) VALUES (%s, %s)", (user_id, blood_type))
            # Update last_response in profile table
            cur.execute("UPDATE profile SET last_response = CURRENT_TIMESTAMP WHERE user_id = %s", (user_id,))
            conn.commit()
            flash(f"Thank you for responding, {email}! Your response for {blood_type} has been logged.", "success")
        else:
            flash(f"Could not find a user with email {email} to log response.", "warning")
    except Error as e:
        print(f"Error logging donor response: {e}")
        flash("An error occurred while logging your response.", "danger")
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()
    return redirect(url_for('home'))

@app.route('/test-mail')
def test_mail_route():
    # For security, only allow logged-in users to trigger this.
    if 'user_id' not in session:
        flash("You must be logged in to perform this test.", "warning")
        return redirect(url_for('home'))

    test_recipient = app.config.get('MAIL_USERNAME')
    if not test_recipient:
        flash("MAIL_USERNAME is not configured in your environment. Cannot run the mail test.", "danger")

    subject = "Oasis - Mail Configuration Test"
    body = "This is a test email from your Oasis application. If you received this, your email configuration is working correctly."
    msg = Message(subject=subject, sender=app.config['MAIL_USERNAME'], recipients=[test_recipient])
    msg.body = body

    try:
        mail.send(msg)
        flash(f"Test email sent successfully to {test_recipient}. Please check your inbox (and spam folder).", "success")
    except Exception as e:
        print(f"--- MAIL TEST FAILED ---")
        print(f"Error: {e}")
        flash(f"Failed to send test email. Error: {e}", "danger")
    
    return redirect(url_for('home'))


DATA_FILE = Path(app.root_path) / 'data' / 'mock_db.json'
DEMO_ADMIN_EMAIL = 'jinnaramuttej@gmail.com'
DEMO_ADMIN_PASSWORD = '123456'


def parse_date(value):
    return date.fromisoformat(value) if value else None


def parse_time(value):
    return time.fromisoformat(value) if value else None


def parse_dt(value):
    return datetime.fromisoformat(value) if value else None


def iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


class MockStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save(self._seed())
        self.sync_admin_defaults()

    def _seed(self):
        return {
            'admins': [{
                'admin_id': 1,
                'name': 'Oasis Admin',
                'email': DEMO_ADMIN_EMAIL,
                'password': generate_password_hash(DEMO_ADMIN_PASSWORD),
                'last_login': None,
            }],
            'profiles': [
                {'user_id': 1, 'username': 'Aarav Mehta', 'email': 'aarav@oasis.local', 'password': generate_password_hash('donor123'), 'created_at': '2026-03-01T09:00:00', 'last_response': '2026-03-29T11:40:00'},
                {'user_id': 2, 'username': 'Priya Sharma', 'email': 'priya@oasis.local', 'password': generate_password_hash('donor123'), 'created_at': '2026-03-03T10:15:00', 'last_response': '2026-03-30T18:10:00'},
                {'user_id': 3, 'username': 'Rohan Patel', 'email': 'rohan@oasis.local', 'password': generate_password_hash('donor123'), 'created_at': '2026-03-05T13:25:00', 'last_response': None},
                {'user_id': 4, 'username': 'Sara Khan', 'email': 'sara@oasis.local', 'password': generate_password_hash('donor123'), 'created_at': '2026-03-07T16:45:00', 'last_response': '2026-03-31T09:05:00'},
            ],
            'donors': [
                {'donor_id': 1, 'profile_id': 1, 'name': 'Aarav Mehta', 'dob': '1993-05-12', 'gender': 'Male', 'contact': '9876543210', 'pincode': '560001', 'blood_group': 'O+', 'profile_picture_url': None, 'verified': True, 'status': 'active'},
                {'donor_id': 2, 'profile_id': 2, 'name': 'Priya Sharma', 'dob': '1995-11-21', 'gender': 'Female', 'contact': '9123456780', 'pincode': '560001', 'blood_group': 'A+', 'profile_picture_url': 'uploads/profile_pics/user_2_pfp.jpg', 'verified': True, 'status': 'active'},
                {'donor_id': 3, 'profile_id': 3, 'name': 'Rohan Patel', 'dob': '1990-01-09', 'gender': 'Male', 'contact': '9988776655', 'pincode': '560034', 'blood_group': 'B+', 'profile_picture_url': None, 'verified': False, 'status': 'pending'},
                {'donor_id': 4, 'profile_id': 4, 'name': 'Sara Khan', 'dob': '1998-08-03', 'gender': 'Female', 'contact': '9090909090', 'pincode': '560078', 'blood_group': 'AB-', 'profile_picture_url': None, 'verified': True, 'status': 'active'},
            ],
            'donations': [
                {'donation_id': 1, 'donor_id': 1, 'donation_date': '2026-01-14', 'location': 'Manipal Hospital', 'units': 1},
                {'donation_id': 2, 'donor_id': 1, 'donation_date': '2026-02-20', 'location': 'Red Cross Camp', 'units': 1},
                {'donation_id': 3, 'donor_id': 1, 'donation_date': '2026-03-22', 'location': 'City Care Drive', 'units': 1},
                {'donation_id': 4, 'donor_id': 2, 'donation_date': '2026-01-28', 'location': 'Apollo Outreach', 'units': 1},
                {'donation_id': 5, 'donor_id': 2, 'donation_date': '2026-03-10', 'location': 'Red Cross Camp', 'units': 1},
                {'donation_id': 6, 'donor_id': 4, 'donation_date': '2026-02-05', 'location': 'Community Health Center', 'units': 1},
            ],
            'messages': [
                {'message_id': 1, 'sender_id': 1, 'receiver_id': 2, 'subject': 'Blood Request for A+', 'body': 'Patient Name: Meera Joshi\nRequired Units: 2\nHospital: City General Hospital\nContact: 9988001122\nReason: Scheduled surgery', 'created_at': '2026-04-01T10:45:00', 'is_read': True},
                {'message_id': 2, 'sender_id': 2, 'receiver_id': 1, 'subject': 'Re: Blood Request', 'body': 'I can help after 4 PM today. Please confirm the ward details.', 'created_at': '2026-04-01T11:05:00', 'is_read': False},
            ],
            'blood_requests': [
                {'request_id': 1, 'requester_id': 1, 'recipient_id': 2, 'patient_name': 'Meera Joshi', 'blood_group': 'A+', 'units': 2, 'hospital': 'City General Hospital', 'contact_person': 'Aarav Mehta', 'contact_phone': '9988001122', 'reason': 'Scheduled surgery', 'pincode': '560001', 'mode': 'direct', 'status': 'sent', 'created_at': '2026-04-01T10:45:00'},
            ],
            'blood_drives': [
                {'drive_id': 1, 'name': 'Spring Lifeline Drive', 'organizer': 'Oasis Community', 'drive_date': '2026-04-10', 'start_time': '09:00:00', 'end_time': '14:00:00', 'location': 'Indiranagar Community Hall', 'description': 'Walk-in blood drive with free health screening and donor refreshments.'},
                {'drive_id': 2, 'name': 'Corporate Donor Day', 'organizer': 'HealthFirst Foundation', 'drive_date': '2026-04-18', 'start_time': '10:30:00', 'end_time': '16:30:00', 'location': 'Electronic City Convention Center', 'description': 'Reserved donation slots for office teams with medical staff on site.'},
            ],
            'emergency_alerts': [
                {'alert_id': 1, 'triggered_by_user_id': 4, 'blood_group_needed': 'O+', 'pincode': '560001', 'contact_phone': '9000011111', 'created_at': '2026-03-31T20:15:00'},
            ],
            'responses': [
                {'response_id': 1, 'user_id': 2, 'blood_type_needed': 'O+', 'created_at': '2026-03-31T20:45:00'},
            ],
            'counters': {'admin_id': 2, 'user_id': 5, 'donor_id': 5, 'donation_id': 7, 'message_id': 3, 'request_id': 2, 'drive_id': 3, 'alert_id': 2, 'response_id': 2},
        }

    def _load(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def _save(self, data):
        self.path.write_text(json.dumps(data, indent=2), encoding='utf-8')

    def sync_admin_defaults(self):
        def fn(data):
            admins = data.setdefault('admins', [])
            if admins:
                admins[0]['email'] = DEMO_ADMIN_EMAIL
                admins[0]['password'] = generate_password_hash(DEMO_ADMIN_PASSWORD)
                admins[0]['name'] = admins[0].get('name') or 'Oasis Admin'
            else:
                admins.append({
                    'admin_id': 1,
                    'name': 'Oasis Admin',
                    'email': DEMO_ADMIN_EMAIL,
                    'password': generate_password_hash(DEMO_ADMIN_PASSWORD),
                    'last_login': None,
                })
                data.setdefault('counters', {})
                data['counters']['admin_id'] = max(data['counters'].get('admin_id', 1), 2)
        self.mutate(fn)

    def read(self):
        with self.lock:
            return self._load()

    def mutate(self, fn):
        with self.lock:
            data = self._load()
            result = fn(data)
            self._save(data)
            return result

    def next_id(self, data, key):
        value = data['counters'][key]
        data['counters'][key] += 1
        return value

    def profile_index(self, data):
        return {item['user_id']: item for item in data['profiles']}

    def donor_index(self, data):
        return {item['profile_id']: item for item in data['donors']}

    def hydrate_profile(self, item):
        if not item:
            return None
        data = dict(item)
        data['created_at'] = parse_dt(data.get('created_at'))
        data['last_response'] = parse_dt(data.get('last_response'))
        return data

    def hydrate_donor(self, item):
        if not item:
            return None
        data = dict(item)
        data['dob'] = parse_date(data.get('dob'))
        return data

    def hydrate_donation(self, item):
        data = dict(item)
        data['donation_date'] = parse_date(data.get('donation_date'))
        return data

    def hydrate_message(self, item):
        data = dict(item)
        data['created_at'] = parse_dt(data.get('created_at'))
        return data

    def get_unread_count(self, user_id):
        return sum(1 for item in self.read()['messages'] if item['receiver_id'] == user_id and not item['is_read'])

    def auth_user(self, identifier, password):
        data = self.read()
        donors = self.donor_index(data)
        ident = identifier.strip().lower()
        for profile in data['profiles']:
            if profile['username'].lower() == ident or profile['email'].lower() == ident:
                if check_password_hash(profile['password'], password):
                    merged = self.hydrate_profile(profile)
                    donor = donors.get(profile['user_id'], {})
                    merged.update(self.hydrate_donor(donor) or {})
                    return merged
        return None

    def create_user(self, payload):
        def fn(data):
            for profile in data['profiles']:
                if profile['username'].lower() == payload['username'].lower():
                    return False, 'Username already exists. Please choose a different name.'
                if profile['email'].lower() == payload['email'].lower():
                    return False, 'Email already exists. Please choose a different email.'
            user_id = self.next_id(data, 'user_id')
            donor_id = self.next_id(data, 'donor_id')
            data['profiles'].append({'user_id': user_id, 'username': payload['username'], 'email': payload['email'].lower(), 'password': generate_password_hash(payload['password']), 'created_at': datetime.now().isoformat(timespec='seconds'), 'last_response': None})
            data['donors'].append({'donor_id': donor_id, 'profile_id': user_id, 'name': payload['username'], 'dob': payload['dob'], 'gender': payload['gender'], 'contact': payload['contact'], 'pincode': payload['pincode'], 'blood_group': payload['blood_group'], 'profile_picture_url': None, 'verified': False, 'status': 'pending'})
            return True, 'Signup successful! Please log in.'
        return self.mutate(fn)

    def update_user(self, user_id, username=None, email=None, donor_updates=None):
        donor_updates = donor_updates or {}

        def fn(data):
            profile = next((item for item in data['profiles'] if item['user_id'] == user_id), None)
            donor = next((item for item in data['donors'] if item['profile_id'] == user_id), None)
            if not profile or not donor:
                return False, 'User not found.'
            if username:
                for item in data['profiles']:
                    if item['user_id'] != user_id and item['username'].lower() == username.lower():
                        return False, 'That name is already in use by another account.'
                profile['username'] = username
                donor['name'] = donor_updates.get('name', username)
            if email:
                lowered = email.lower()
                for item in data['profiles']:
                    if item['user_id'] != user_id and item['email'].lower() == lowered:
                        return False, 'That email address is already in use.'
                profile['email'] = lowered
            for key, value in donor_updates.items():
                donor[key] = iso(value)
            return True, 'Profile updated successfully!'
        return self.mutate(fn)

    def get_donor(self, user_id):
        data = self.read()
        return self.hydrate_donor(self.donor_index(data).get(user_id))

    def get_profile_with_history(self, user_id):
        data = self.read()
        profile = self.profile_index(data).get(user_id)
        donor = self.donor_index(data).get(user_id)
        if not profile or not donor:
            return None, []
        merged = self.hydrate_profile(profile)
        merged.update(self.hydrate_donor(donor))
        history = [self.hydrate_donation(item) for item in data['donations'] if item['donor_id'] == donor['donor_id']]
        history.sort(key=lambda item: item['donation_date'] or date.min, reverse=True)
        return merged, history

    def user_dashboard(self, user_id):
        user, history = self.get_profile_with_history(user_id)
        conversations = self.conversations(user_id)
        drives = self.blood_drives()[:3]
        return {
            'user': user,
            'recent_donations': history[:3],
            'recent_conversations': conversations[:4],
            'upcoming_drives': drives,
            'stats': {
                'donations': len(history),
                'messages': len(conversations),
                'unread': self.get_unread_count(user_id),
                'verified': bool(user and user.get('verified')),
            }
        }

    def search_donors(self, blood_group, pincode, exclude_user_id=None):
        data = self.read()
        profiles = self.profile_index(data)
        totals = {}
        for item in data['donations']:
            totals[item['donor_id']] = totals.get(item['donor_id'], 0) + 1
        results = []
        for donor in data['donors']:
            if blood_group and donor['blood_group'] != blood_group:
                continue
            if pincode and donor['pincode'] != pincode:
                continue
            if exclude_user_id and donor['profile_id'] == exclude_user_id:
                continue
            profile = profiles.get(donor['profile_id'])
            if not profile:
                continue
            item = self.hydrate_donor(donor)
            item.update({'user_id': donor['profile_id'], 'email': profile['email'], 'username': profile['username'], 'donation_count': totals.get(donor['donor_id'], 0)})
            results.append(item)
        results.sort(key=lambda item: (not item['verified'], -item['donation_count'], item['name']))
        return results

    def get_public_donor(self, user_id):
        data = self.read()
        profile = self.profile_index(data).get(user_id)
        donor = self.donor_index(data).get(user_id)
        if not profile or not donor:
            return None
        item = self.hydrate_profile(profile)
        item.update(self.hydrate_donor(donor))
        item['user_id'] = user_id
        return item

    def leaderboard(self):
        data = self.read()
        donors = {item['donor_id']: item for item in data['donors']}
        totals = {}
        for item in data['donations']:
            totals[item['donor_id']] = totals.get(item['donor_id'], 0) + 1
        leaders = []
        for donor_id, count in totals.items():
            donor = donors.get(donor_id)
            if donor:
                leaders.append({'name': donor['name'], 'pincode': donor['pincode'], 'donations': count})
        leaders.sort(key=lambda item: (-item['donations'], item['name']))
        for index, leader in enumerate(leaders):
            leader['prize'] = f"${max(0, 300 - index * 100)}" if index < 3 else '---'
        return leaders[:10]

    def blood_drives(self):
        drives = []
        for item in self.read()['blood_drives']:
            drive = dict(item)
            drive['drive_date'] = parse_date(drive.get('drive_date'))
            drive['start_time'] = parse_time(drive.get('start_time'))
            drive['end_time'] = parse_time(drive.get('end_time'))
            if drive['drive_date'] and drive['drive_date'] >= date.today():
                drives.append(drive)
        drives.sort(key=lambda item: item['drive_date'])
        return drives

    def create_request_and_messages(self, requester_id, recipient_ids, payload, mode):
        now = datetime.now().isoformat(timespec='seconds')

        def fn(data):
            count = 0
            for recipient_id in recipient_ids:
                data['blood_requests'].append({
                    'request_id': self.next_id(data, 'request_id'),
                    'requester_id': requester_id,
                    'recipient_id': recipient_id,
                    'patient_name': payload['patient_name'],
                    'blood_group': payload['blood_group'],
                    'units': int(payload['units']),
                    'hospital': payload['hospital'],
                    'contact_person': payload['contact_person'],
                    'contact_phone': payload['contact_phone'],
                    'reason': payload['reason'],
                    'pincode': payload['pincode'],
                    'mode': mode,
                    'status': 'sent',
                    'created_at': now,
                })
                data['messages'].append({
                    'message_id': self.next_id(data, 'message_id'),
                    'sender_id': requester_id,
                    'receiver_id': recipient_id,
                    'subject': f"Blood Request for {payload['blood_group']}",
                    'body': payload['body'],
                    'created_at': now,
                    'is_read': False,
                })
                count += 1
            return count
        return self.mutate(fn)

    def send_message(self, sender_id, receiver_id, subject, body):
        def fn(data):
            item = {
                'message_id': self.next_id(data, 'message_id'),
                'sender_id': sender_id,
                'receiver_id': receiver_id,
                'subject': subject,
                'body': body,
                'created_at': datetime.now().isoformat(timespec='seconds'),
                'is_read': False,
            }
            data['messages'].append(item)
            return item
        return self.hydrate_message(self.mutate(fn))

    def conversations(self, user_id):
        data = self.read()
        profiles = self.profile_index(data)
        donors = self.donor_index(data)
        latest = {}
        for item in data['messages']:
            if user_id not in (item['sender_id'], item['receiver_id']):
                continue
            partner = item['receiver_id'] if item['sender_id'] == user_id else item['sender_id']
            if partner not in latest or item['created_at'] > latest[partner]['created_at']:
                latest[partner] = item
        rows = []
        for partner_id, item in latest.items():
            profile = profiles.get(partner_id)
            donor = donors.get(partner_id, {})
            if not profile:
                continue
            rows.append({'user_id': partner_id, 'username': profile['username'], 'profile_picture_url': donor.get('profile_picture_url'), 'body': item['body'], 'created_at': parse_dt(item['created_at']), 'is_read': item['is_read'], 'sender_id': item['sender_id']})
        rows.sort(key=lambda item: item['created_at'] or datetime.min, reverse=True)
        return rows

    def conversation(self, user_id, other_user_id):
        def fn(data):
            profiles = self.profile_index(data)
            if other_user_id not in profiles:
                return [], None
            for item in data['messages']:
                if item['receiver_id'] == user_id and item['sender_id'] == other_user_id:
                    item['is_read'] = True
            rows = []
            for item in data['messages']:
                if {item['sender_id'], item['receiver_id']} == {user_id, other_user_id}:
                    row = self.hydrate_message(item)
                    row['sender_username'] = profiles.get(item['sender_id'], {}).get('username', 'Unknown')
                    rows.append(row)
            rows.sort(key=lambda item: item['created_at'] or datetime.min)
            return rows, {'username': profiles[other_user_id]['username']}
        return self.mutate(fn)

    def log_alert(self, user_id, blood_group, pincode, contact_phone):
        def fn(data):
            data['emergency_alerts'].append({'alert_id': self.next_id(data, 'alert_id'), 'triggered_by_user_id': user_id, 'blood_group_needed': blood_group, 'pincode': pincode, 'contact_phone': contact_phone, 'created_at': datetime.now().isoformat(timespec='seconds')})
        self.mutate(fn)

    def users_by_blood(self, blood_group, pincode):
        return [(item['email'], item['contact']) for item in self.search_donors(blood_group, pincode)]

    def all_users(self):
        data = self.read()
        profiles = self.profile_index(data)
        return [(profiles[item['profile_id']]['email'], item['blood_group'], item['contact']) for item in data['donors'] if item['profile_id'] in profiles]

    def log_response(self, email, blood_group):
        def fn(data):
            profile = next((item for item in data['profiles'] if item['email'].lower() == email.lower()), None)
            if not profile:
                return False
            data['responses'].append({'response_id': self.next_id(data, 'response_id'), 'user_id': profile['user_id'], 'blood_type_needed': blood_group, 'created_at': datetime.now().isoformat(timespec='seconds')})
            profile['last_response'] = datetime.now().isoformat(timespec='seconds')
            return True
        return self.mutate(fn)

    def auth_admin(self, email, password):
        for admin in self.read()['admins']:
            if admin['email'].lower() == email.lower() and check_password_hash(admin['password'], password):
                row = dict(admin)
                row['last_login'] = parse_dt(row.get('last_login'))
                return row
        return None

    def touch_admin_login(self, admin_id):
        def fn(data):
            admin = next((item for item in data['admins'] if item['admin_id'] == admin_id), None)
            if admin:
                admin['last_login'] = datetime.now().isoformat(timespec='seconds')
        self.mutate(fn)

    def dashboard(self):
        data = self.read()
        profiles = self.profile_index(data)
        donors = self.donor_index(data)
        donation_totals = {}
        for item in data['donations']:
            donation_totals[item['donor_id']] = donation_totals.get(item['donor_id'], 0) + 1
        users = []
        for profile in data['profiles']:
            donor = donors.get(profile['user_id'], {})
            users.append({
                **self.hydrate_profile(profile),
                **(self.hydrate_donor(donor) or {}),
                'donation_count': donation_totals.get(donor.get('donor_id', -1), 0),
            })
        users.sort(key=lambda item: item['created_at'] or datetime.min, reverse=True)
        requests = []
        for item in data['blood_requests']:
            row = dict(item)
            row['created_at'] = parse_dt(row.get('created_at'))
            row['requester_name'] = profiles.get(item['requester_id'], {}).get('username', 'Unknown')
            row['recipient_name'] = profiles.get(item['recipient_id'], {}).get('username', 'Unknown')
            requests.append(row)
        requests.sort(key=lambda item: item['created_at'] or datetime.min, reverse=True)
        alerts = []
        for item in data['emergency_alerts']:
            row = dict(item)
            row['created_at'] = parse_dt(row.get('created_at'))
            row['triggered_by_name'] = profiles.get(item['triggered_by_user_id'], {}).get('username', 'Unknown')
            alerts.append(row)
        alerts.sort(key=lambda item: item['created_at'] or datetime.min, reverse=True)
        admins = []
        for item in data['admins']:
            row = dict(item)
            row['last_login'] = parse_dt(row.get('last_login'))
            admins.append(row)
        return {
            'stats': {
                'total_users': len(data['profiles']),
                'verified_donors': sum(1 for item in data['donors'] if item.get('verified')),
                'open_requests': len(data['blood_requests']),
                'emergency_alerts': len(data['emergency_alerts']),
                'messages': len(data['messages']),
                'responses': len(data['responses']),
            },
            'users': users,
            'requests': requests,
            'alerts': alerts,
            'admins': admins,
        }


mock_store = MockStore(DATA_FILE)


def login_user_session(user):
    session.clear()
    session['user_id'] = user['user_id']
    session['username'] = user['username']
    session['profile_pic_url'] = user.get('profile_picture_url')


def login_admin_session(admin):
    session.clear()
    session['is_admin'] = True
    session['admin_id'] = admin['admin_id']
    session['admin_name'] = admin['name']


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Please sign in as an admin to view that page.', 'warning')
            return redirect(url_for('admin_login_page'))
        return view(*args, **kwargs)
    return wrapped


def request_body(payload):
    return (
        f"Patient Name: {payload['patient_name']}\n"
        f"Required Units: {payload['units']}\n"
        f"Hospital: {payload['hospital']}\n"
        f"Contact Person: {payload['contact_person']}\n"
        f"Contact Phone: {payload['contact_phone']}\n"
        f"Reason: {payload['reason'] or 'Not provided'}"
    )


def mock_get_involved_page():
    if request.method == 'POST':
        form_type = request.form.get('form_type')
        if form_type == 'donor_update':
            if 'user_id' not in session:
                flash('Please log in to update your profile.', 'warning')
                return redirect(url_for('get_involved_page'))
            username = request.form.get('name', '').strip()
            success, message = mock_store.update_user(session['user_id'], username=username, donor_updates={
                'name': username,
                'dob': request.form.get('dob'),
                'gender': request.form.get('gender'),
                'contact': request.form.get('contact'),
                'pincode': request.form.get('pincode', '').strip(),
                'blood_group': request.form.get('bloodgroup'),
                'status': 'active',
            })
            flash(message, 'success' if success else 'warning')
            if success:
                session['username'] = username
                return redirect(url_for('my_profile_page'))
        elif form_type == 'signup':
            payload = {
                'username': request.form.get('signup-name', '').strip(),
                'email': request.form.get('signup-email', '').strip().lower(),
                'password': request.form.get('signup-password', ''),
                'dob': request.form.get('signup-dob'),
                'gender': request.form.get('signup-gender'),
                'contact': request.form.get('signup-contact'),
                'pincode': request.form.get('signup-pincode', '').strip(),
                'blood_group': request.form.get('signup-bloodgroup'),
            }
            confirm = request.form.get('signup-confirm-password', '')
            if not all(payload.values()) or not confirm:
                flash('Please fill all signup fields.', 'warning')
            elif payload['password'] != confirm:
                flash('Passwords do not match.', 'warning')
            else:
                success, message = mock_store.create_user(payload)
                flash(message, 'success' if success else 'warning')
                if success:
                    user = mock_store.auth_user(payload['email'], payload['password'])
                    if user:
                        login_user_session(user)
                        flash(f"Welcome, {user['username']}!", 'success')
                        return redirect(url_for('dashboard_page'))
        elif form_type == 'login':
            user = mock_store.auth_user(request.form.get('login-email', ''), request.form.get('login-password', ''))
            if user:
                login_user_session(user)
                flash(f"Welcome back, {user['username']}!", 'success')
                return redirect(url_for('dashboard_page'))
            flash('Invalid credentials. Please try again.', 'danger')
    donor_data = mock_store.get_donor(session['user_id']) if 'user_id' in session else None
    return render_template('get-involved.html', donor_data=donor_data)


def mock_my_profile_page():
    if 'user_id' not in session:
        flash('Please log in to view this page.', 'warning')
        return redirect(url_for('get_involved_page'))
    user_id = session['user_id']
    if request.method == 'POST':
        fullname = request.form.get('fullName', '').strip()
        donor_updates = {
            'name': fullname,
            'contact': request.form.get('phone', '').strip(),
            'pincode': request.form.get('pincode', '').strip(),
            'dob': request.form.get('dob'),
            'blood_group': request.form.get('bloodgroup'),
            'status': 'active',
        }
        profile_pic = request.files.get('profile-picture')
        if profile_pic and profile_pic.filename:
            if not allowed_file(profile_pic.filename):
                flash('Please upload a valid image file.', 'warning')
                return redirect(url_for('my_profile_page'))
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(f"user_{user_id}_{profile_pic.filename}")
            profile_pic.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            donor_updates['profile_picture_url'] = f"uploads/profile_pics/{filename}"
            session['profile_pic_url'] = donor_updates['profile_picture_url']
        success, message = mock_store.update_user(user_id, username=fullname, email=request.form.get('email', '').strip().lower(), donor_updates=donor_updates)
        flash(message, 'success' if success else 'warning')
        if success:
            session['username'] = fullname
        return redirect(url_for('my_profile_page'))
    user, donation_history = mock_store.get_profile_with_history(user_id)
    return render_template('my-profile.html', user=user, donation_history=donation_history)


def mock_search_donors_page():
    donors = []
    if request.method == 'POST':
        blood_group = request.form.get('bloodgroup')
        pincode = request.form.get('pincode', '').strip()
        donors = mock_store.search_donors(blood_group, pincode, session.get('user_id'))
        if not donors:
            flash(f"No donors found for blood group {blood_group} in PIN code {pincode}.", 'info')
    return render_template('search-donors.html', donors=donors)


def mock_leaderboard_page():
    return render_template('leaderboard.html', leaders=mock_store.leaderboard())


def mock_blood_request_page():
    if 'user_id' not in session:
        flash('Please log in to make a blood request.', 'warning')
        return redirect(url_for('get_involved_page'))
    if request.method == 'POST':
        blood_group = request.form.get('bloodgroup')
        pincode = request.form.get('pincode', '').strip()
        recipients = mock_store.search_donors(blood_group, pincode, session['user_id'])
        if not recipients:
            flash(f"No donors found for blood group {blood_group} in PIN code {pincode}. Your request was not sent.", 'info')
            return redirect(url_for('blood_request_page'))
        payload = {
            'patient_name': request.form.get('patient-name', '').strip(),
            'blood_group': blood_group,
            'units': request.form.get('units', '1'),
            'hospital': request.form.get('hospital', '').strip(),
            'contact_person': request.form.get('contact-person', '').strip(),
            'contact_phone': request.form.get('contact-phone', '').strip(),
            'reason': request.form.get('reason', '').strip(),
            'pincode': pincode,
        }
        payload['body'] = request_body(payload)
        count = mock_store.create_request_and_messages(session['user_id'], [item['user_id'] for item in recipients], payload, 'bulk')
        flash(f"Your request has been sent as an internal message to {count} matching donor(s)!", 'success')
        return redirect(url_for('home'))
    return render_template('blood-request.html')


def mock_blood_drives_page():
    return render_template('blood-drives.html', drives=mock_store.blood_drives())


@app.route('/dashboard')
def dashboard_page():
    if 'user_id' not in session:
        flash('Please log in to view your dashboard.', 'warning')
        return redirect(url_for('get_involved_page'))
    return render_template('dashboard.html', **mock_store.user_dashboard(session['user_id']))


def mock_admin_login_page():
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        admin = mock_store.auth_admin(request.form.get('email', '').strip(), request.form.get('password', ''))
        if admin:
            mock_store.touch_admin_login(admin['admin_id'])
            login_admin_session(admin)
            flash('Admin login successful.', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Invalid admin credentials.', 'danger')
    return render_template('admin-login.html', demo_admin_email=DEMO_ADMIN_EMAIL, demo_admin_password=DEMO_ADMIN_PASSWORD)


@app.route('/admin')
@admin_required
def admin_dashboard():
    return render_template('admin-panel.html', **mock_store.dashboard())


def mock_emergency_request_page():
    if 'user_id' not in session:
        flash('You must be logged in to send an emergency alert.', 'warning')
        return redirect(url_for('get_involved_page'))
    if request.method == 'POST':
        blood_type = request.form.get('bloodgroup')
        pincode = request.form.get('pincode', '').strip()
        contact_phone = request.form.get('contact-phone', '').strip()
        if not all([blood_type, pincode, contact_phone]):
            flash('Blood group, PIN code, and a contact phone are required for an emergency alert.', 'danger')
            return redirect(url_for('emergency_request_page'))
        mock_store.log_alert(session['user_id'], blood_type, pincode, contact_phone)
        socketio.emit('emergency_alert', {'blood_type': blood_type, 'pincode': pincode})
        recipients = mock_store.users_by_blood(blood_type, pincode)
        if recipients:
            flash(f"Emergency alert for {blood_type} logged for {len(recipients)} matching donors in the local mock store.", 'success')
        else:
            flash(f"No exact match found for {blood_type} in {pincode}. Alert logged for admin follow-up.", 'warning')
        return redirect(url_for('home'))
    return render_template('emergency-request.html')


def mock_request_donor_page(donor_id):
    if 'user_id' not in session:
        flash('You must be logged in to make a request.', 'warning')
        return redirect(url_for('get_involved_page'))
    donor = mock_store.get_public_donor(donor_id)
    if not donor:
        flash('Donor not found.', 'danger')
        return redirect(url_for('search_donors_page'))
    if request.method == 'POST':
        payload = {
            'patient_name': request.form.get('patient-name', '').strip(),
            'blood_group': request.form.get('blood_group'),
            'units': request.form.get('units', '1'),
            'hospital': request.form.get('hospital', '').strip(),
            'contact_person': session.get('username', 'Requester'),
            'contact_phone': request.form.get('contact-phone', '').strip(),
            'reason': request.form.get('reason', '').strip(),
            'pincode': donor.get('pincode', ''),
        }
        payload['body'] = request_body(payload)
        mock_store.create_request_and_messages(session['user_id'], [donor_id], payload, 'direct')
        receiver_sid = online_users.get(donor_id)
        if receiver_sid:
            emit('new_message', {'sender_id': session['user_id'], 'sender_username': session.get('username'), 'body': payload['body'], 'created_at': datetime.now().strftime('%b %d, %I:%M %p')}, to=receiver_sid)
        flash('Your request has been sent to the donor!', 'success')
        return redirect(url_for('conversation_page', other_user_id=donor_id))
    return render_template('request-donor.html', donor=donor)


def mock_inbox_page():
    if 'user_id' not in session:
        flash('Please log in to view your inbox.', 'warning')
        return redirect(url_for('get_involved_page'))
    return render_template('inbox.html', conversations=mock_store.conversations(session['user_id']))


def mock_conversation_page(other_user_id):
    if 'user_id' not in session:
        return redirect(url_for('get_involved_page'))
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if body:
            mock_store.send_message(session['user_id'], other_user_id, 'Re: Blood Request', body)
            receiver_sid = online_users.get(other_user_id)
            if receiver_sid:
                emit('new_message', {'sender_id': session['user_id'], 'sender_username': session.get('username'), 'body': body, 'created_at': datetime.now().strftime('%b %d, %I:%M %p')}, to=receiver_sid)
        return redirect(url_for('conversation_page', other_user_id=other_user_id))
    messages, other_user = mock_store.conversation(session['user_id'], other_user_id)
    if not other_user:
        flash('Conversation could not be found.', 'danger')
        return redirect(url_for('inbox_page'))
    return render_template('conversation.html', messages=messages, other_user=other_user, other_user_id=other_user_id)


def mock_donor_response():
    email = request.args.get('email')
    blood_type = request.args.get('blood_type')
    if not email or not blood_type:
        flash('Invalid response link.', 'danger')
    elif mock_store.log_response(email, blood_type):
        flash(f"Thank you for responding, {email}! Your response for {blood_type} has been logged.", 'success')
    else:
        flash(f"Could not find a user with email {email} to log response.", 'warning')
    return redirect(url_for('home'))


def mock_test_mail_route():
    if 'user_id' not in session and not session.get('is_admin'):
        flash('You must be logged in to perform this test.', 'warning')
        return redirect(url_for('home'))
    if not app.config.get('MAIL_USERNAME'):
        flash('Mail is not configured. Local mock mode is active.', 'info')
        return redirect(url_for('home'))
    flash(f"Mail is configured for {app.config.get('MAIL_USERNAME')}. The local mock setup leaves delivery disabled by default.", 'info')
    return redirect(url_for('home'))


def mock_get_users_by_blood_type(blood_type, pincode):
    return mock_store.users_by_blood(blood_type, pincode)


def mock_get_all_users():
    return mock_store.all_users()


def mock_get_donors_for_request(blood_type, pincode, exclude_user_id=None):
    return mock_store.search_donors(blood_type, pincode, exclude_user_id)


app.view_functions['get_involved_page'] = mock_get_involved_page
app.view_functions['my_profile_page'] = mock_my_profile_page
app.view_functions['search_donors_page'] = mock_search_donors_page
app.view_functions['leaderboard_page'] = mock_leaderboard_page
app.view_functions['blood_request_page'] = mock_blood_request_page
app.view_functions['blood_drives_page'] = mock_blood_drives_page
app.view_functions['admin_login_page'] = mock_admin_login_page
app.view_functions['emergency_request_page'] = mock_emergency_request_page
app.view_functions['request_donor_page'] = mock_request_donor_page
app.view_functions['inbox_page'] = mock_inbox_page
app.view_functions['conversation_page'] = mock_conversation_page
app.view_functions['donor_response'] = mock_donor_response
app.view_functions['test_mail_route'] = mock_test_mail_route
globals()['get_users_by_blood_type'] = mock_get_users_by_blood_type
globals()['get_all_users'] = mock_get_all_users
globals()['get_donors_for_request'] = mock_get_donors_for_request

if __name__ == "__main__":
    socketio.run(app, debug=True)
