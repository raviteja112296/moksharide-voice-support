from flask import Flask, request, Response
from flask_cors import CORS
from twilio.twiml.voice_response import VoiceResponse, Gather
from groq import Groq
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
CORS(app)

# ── Clients ───────────────────────────────────────────────────
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

# ── Conversation History (per call) ───────────────────────────
call_history = {}

# ── MokshaRide Voice System Prompt ────────────────────────────
SYSTEM_PROMPT = """
You are Yatri, a friendly voice support assistant for MokshaRide,
a ride booking app in India.

You are on a PHONE CALL with the user.
So follow these rules strictly:

1. Keep replies SHORT — maximum 2-3 sentences
   User is on a call, not reading text.

2. Speak naturally like a human.
   No bullet points, no lists.
   Just natural conversation.

3. Reply in the same language user speaks.
   Hindi → reply in Hindi
   English → reply in English
   Hinglish → reply in Hinglish

4. Always be warm, calm and empathetic.

You help with:
- Ride cancellations and refunds
- Driver not arriving
- Fare complaints
- OTP problems
- App technical issues
- General ride booking help

Refund Policy:
- Driver cancels → full refund in 3-5 business days
- User cancels within 2 min → no charge
- User cancels after 2 min → small fee applies
- Overcharge → refund after verification in 24 hours

Ride fares:
- Auto → Rs.30 base + Rs.15 per km
- Cab  → Rs.50 base + Rs.22 per km
- Bike → Rs.30 base + Rs.20 per km

If you cannot solve → say:
"I am connecting you to our team.
 You will hear back within 24 hours."

Never make up information.
Never share other user data.
Keep every reply under 3 sentences.
"""


# ── Health Check ──────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "message": "MokshaRide Voice AI is running."}


# ── Step 1: User calls your Twilio number ─────────────────────
@app.route("/voice", methods=["GET", "POST"])
def voice():
    call_sid = request.form.get("CallSid", "unknown")

    # Initialize conversation for this call
    call_history[call_sid] = []

    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="/voice-reply",
        method="POST",
        timeout=5,
        speech_timeout="auto",
        language="en-IN",
        hints="auto, cab, bike, ride, cancel, refund, driver, OTP"
    )

    gather.say(
        "Welcome to MokshaRide Support. "
        "I am Yatri, your support assistant. "
        "How can I help you today?",
        voice="alice",
        language="en-IN"
    )

    response.append(gather)

    # If user says nothing
    response.say(
        "I did not hear anything. Please call again. Goodbye.",
        voice="alice",
        language="en-IN"
    )

    return Response(str(response), mimetype="text/xml")


# ── Step 2: User spoke → AI understands → replies ─────────────
@app.route("/voice-reply", methods=["GET", "POST"])
def voice_reply():
    call_sid    = request.form.get("CallSid", "unknown")
    spoken_text = request.form.get("SpeechResult", "").strip()
    confidence  = request.form.get("Confidence", "0")

    print(f"[Call: {call_sid}] User said: '{spoken_text}' (confidence: {confidence})")

    response = VoiceResponse()

    # If nothing detected
    if not spoken_text:
        gather = Gather(
            input="speech",
            action="/voice-reply",
            method="POST",
            timeout=5,
            speech_timeout="auto",
            language="en-IN"
        )
        gather.say(
            "Sorry, I did not catch that. Could you please repeat?",
            voice="alice",
            language="en-IN"
        )
        response.append(gather)
        return Response(str(response), mimetype="text/xml")

    # Initialize history if new call
    if call_sid not in call_history:
        call_history[call_sid] = []

    # Add user message to history
    call_history[call_sid].append({
        "role": "user",
        "content": spoken_text
    })

    # Keep last 6 messages only (3 back and forth exchanges)
    recent = call_history[call_sid][-6:]

    # Get AI reply from Groq
    try:
        groq_response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *recent
            ],
            max_tokens=150,
            temperature=0.7,
        )

        ai_reply = groq_response.choices[0].message.content.strip()
        print(f"[Call: {call_sid}] AI reply: '{ai_reply}'")

        # Save AI reply to history
        call_history[call_sid].append({
            "role": "assistant",
            "content": ai_reply
        })

    except Exception as e:
        print(f"[Groq Error] {e}")
        ai_reply = "I am having trouble right now. Please try again shortly."

    # Check if user wants to end call
    end_keywords = [
        "bye", "goodbye", "thank you", "thanks",
        "ok bye", "shukriya", "dhanyawad", "that's all",
        "nothing else", "no thanks"
    ]
    is_ending = any(word in spoken_text.lower() for word in end_keywords)

    if is_ending:
        # End the call
        response.say(ai_reply, voice="alice", language="en-IN")
        response.hangup()

        # Clean up history
        if call_sid in call_history:
            del call_history[call_sid]

    else:
        # Continue conversation — listen for next input
        gather = Gather(
            input="speech",
            action="/voice-reply",
            method="POST",
            timeout=5,
            speech_timeout="auto",
            language="en-IN",
            hints="auto, cab, bike, ride, cancel, refund, driver, OTP"
        )
        gather.say(ai_reply, voice="alice", language="en-IN")
        response.append(gather)

        # If user goes silent after AI speaks
        response.say(
            "Is there anything else I can help you with?",
            voice="alice",
            language="en-IN"
        )

    return Response(str(response), mimetype="text/xml")


# ── Step 3: Call status updates from Twilio ───────────────────
@app.route("/voice-status", methods=["POST"])
def voice_status():
    call_sid    = request.form.get("CallSid", "unknown")
    call_status = request.form.get("CallStatus", "unknown")

    print(f"[Call: {call_sid}] Final status: {call_status}")

    # Clean up when call ends
    if call_status in ["completed", "failed", "busy", "no-answer"]:
        if call_sid in call_history:
            del call_history[call_sid]
            print(f"[Call: {call_sid}] History cleared.")

    return {"status": "ok"}
#for testing and avoid cost and now free upto free tair
from twilio.rest import Client

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route("/make-call", methods=["POST"])
def make_call():
    user_number = request.json.get("phone")

    call = client.calls.create(
        to=user_number,
        from_=TWILIO_PHONE_NUMBER,
        url="https://moksharide-voice-support.onrender.com/voice"
    )

    return {"status": "calling", "call_sid": call.sid}
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 10000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"MokshaRide Voice AI running on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)