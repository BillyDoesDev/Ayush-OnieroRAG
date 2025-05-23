import json
import os
import time
import pytz
import numpy as np
import pandas as pd
# import torch
from dotenv import load_dotenv
from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from scripts import the_big_dipper
from prometheus_client import Counter, Histogram, Summary, Gauge, Info, generate_latest, REGISTRY, CollectorRegistry

# print(torch.cuda.is_available())
# print( torch.cuda.device_count())
# print(torch.cuda.get_device_name(0))

load_dotenv()
app = Flask(__name__)
CORS(app)

# Configure the SQLite Database
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///queries.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
IST = pytz.timezone("Asia/Kolkata")
RESOURCES_DIR = "static/resources"
df = pd.read_csv("static/assets/facebook_dream_archetypes.csv")
dream_text = ""
selected_archetype = ""

# Database Model for Logging Queries, Responses, and Chart Data
class QueryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dream_text = db.Column(db.Text, nullable=False)
    response_data = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(pytz.utc).astimezone(IST))

class ChartData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chart_type = db.Column(db.String(50), nullable=False)
    data = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(pytz.utc).astimezone(IST))



# Define Prometheus metrics
DREAM_SUBMISSIONS = Counter('dream_submissions_total', 'Total number of dreams submitted', ['status'])
ENDPOINT_REQUESTS = Counter('endpoint_requests_total', 'Total requests per endpoint', ['endpoint', 'method', 'status_code'])
REQUEST_LATENCY = Histogram('request_latency_seconds', 'Request latency in seconds', ['endpoint'])
DREAM_PROCESSING_TIME = Summary('dream_processing_seconds', 'Time spent processing dreams')
ACTIVE_USERS = Gauge('active_users', 'Number of active users')
APP_INFO = Info('dream_analyzer', 'Dream analyzer application information')

# Set application info
APP_INFO.info({'version': '1.0.0', 'maintainer': 'Dream Team'})

# Archetype distribution gauge
ARCHETYPE_DISTRIBUTION = Gauge('archetype_distribution', 'Distribution of dream archetypes', ['archetype'])

# Initialize distribution based on data frame
for archetype, count in df['archetype'].value_counts().items():
    ARCHETYPE_DISTRIBUTION.labels(archetype).set(count)



# JSON Encoder for NumPy Types
class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)

app.json_encoder = NumpyEncoder

def json_listify(data: dict) -> str:
    return json.dumps([{"_id_": key, "_text_": value} for key, value in data.items()])


# Generate time series data for archetypes
def generate_time_series_data():
    archetypes = ['explorer', 'everyman', 'hero', 'outlaw', 'sage']
    end_date = datetime.now()
    
    # Generate dates for the past 6 months
    dates = [(end_date - timedelta(days=i*30)).strftime('%Y-%m') for i in range(6)]
    dates.reverse()  # Chronological order
    
    data = []
    for archetype in archetypes:
        # Generate somewhat smooth trend with some randomness
        base_value = np.random.randint(5, 15)
        trend = np.random.choice([-1, 0, 1])  # Trend direction
        values = []
        
        for i in range(len(dates)):
            # Value changes with some trend and randomness
            val = max(1, base_value + trend * i + np.random.randint(-3, 4))
            # Convert NumPy int64 to regular Python int
            val = int(val)
            values.append(val)
        
        data.append({
            'archetype': archetype,
            'values': values
        })
    
    return {
        'dates': dates,
        'data': data
    }



# Calculate rarity score based on archetype distribution
def calculate_rarity_score(archetype):
    # Rarity is inversely proportional to frequency
    # higher the number here, more common it is
    archetype_weights = {
        'explorer': 0.3,
        'everyman': 1,
        'hero': 0.15,
        'outlaw': 0.1,
        'sage': 0.1,
        'creator': 0.1,
        'caregiver': 0.5,
        'lover': 0.7
    }
    
    # Calculate base rarity (rare archetypes = high score)
    base_rarity = 100 - (archetype_weights.get(archetype, 0.5) * 100)
    
    # Add some randomness for variability
    randomness = np.random.normal(0, 10)
    
    # Ensure score is between 0 and 100
    score = max(0, min(100, base_rarity + randomness))
    
    # Convert to native Python float
    score = float(round(score, 1))
    
    return score


# Middleware for tracking request metrics
@app.before_request
def before_request():
    request.start_time = time.time()
    ACTIVE_USERS.inc()

@app.after_request
def after_request(response):
    request_latency = time.time() - request.start_time
    ENDPOINT_REQUESTS.labels(
        endpoint=request.path, 
        method=request.method,
        status_code=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(endpoint=request.path).observe(request_latency)
    ACTIVE_USERS.dec()
    return response



@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

# Prometheus metrics endpoint on the same server
@app.route('/metrics')
def metrics():
    return Response(generate_latest(REGISTRY), mimetype='text/plain')
@app.route("/llm", methods=["POST"])
def llm_():
    global dream_text
    global selected_archetype

    # Track dream processing time
    with DREAM_PROCESSING_TIME.time():
        try:
            if "dream" not in request.form or not request.form["dream"].strip():
                return jsonify({"error": "Dream text is required"}), 400
            
            dream_text = request.form["dream"].strip()
            data = the_big_dipper.main(dream_text=dream_text)
            json_response = json_listify(data)

            new_entry = QueryLog(dream_text=dream_text, response_data=json_response)
            selected_archetype = data['archetype']

            # Update archetype distribution
            ARCHETYPE_DISTRIBUTION.labels(selected_archetype).inc()
            
            # Record successful submission
            DREAM_SUBMISSIONS.labels(status='success').inc()

            db.session.add(new_entry)
            db.session.commit()
            
            return Response(json_response, mimetype="application/json")
        except Exception as e:
            # Record failed submission
            DREAM_SUBMISSIONS.labels(status='error').inc()
            print(f"Error processing dream: {e}")
            return jsonify({"error": "Failed to process dream"}), 500


@app.route("/history/<date>", methods=["GET"])
def get_history_by_date(date):
    try:
        for format_string in ["%a %b %d %Y", "%Y-%m-%d", "%m/%d/%Y"]:
            try:
                date_obj = datetime.strptime(date, format_string).replace(tzinfo=IST)
                break
            except ValueError:
                continue
        else:
            return jsonify({"error": "Invalid date format"}), 400
        
        start_of_day = IST.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0, 0))
        end_of_day = IST.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, 59))
        
        queries = QueryLog.query.filter(QueryLog.timestamp >= start_of_day, QueryLog.timestamp <= end_of_day).order_by(QueryLog.timestamp.desc()).all()
        
        return jsonify([{ "id": q.id, "preview": q.dream_text[:30] + "...", "timestamp": q.timestamp.strftime("%H:%M") } for q in queries])
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/query/<int:query_id>", methods=["GET"])
def get_query_by_id(query_id):
    global dream_text
    global selected_archetype

    query = QueryLog.query.get_or_404(query_id)
    dream_text = query.dream_text

    response_data = json.loads(query.response_data)
    # Look for the archetype in the response data
    for item in response_data:
        if item.get("_id_") == "archetype":
            selected_archetype = item.get("_text_")
            break
    else:
        selected_archetype = ""  # Fallback if not found

    return jsonify({
        "id": query.id,
        "dream_text": query.dream_text,
        "response_data": response_data,
        "timestamp": query.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    })

# Chart Data Storage and Retrieval
@app.route('/get_bar_data')
def get_bar_data():
    # Convert to the format expected by Chart.js
    counts = df['archetype'].value_counts()
    
    # Convert NumPy types to native Python types
    data = {
        'labels': counts.index.tolist(),
        'values': [int(val) for val in counts.values.tolist()]
    }
    
    return jsonify(data)

@app.route("/get_doughnut_data")
def get_doughnut_data():
    __v__ = the_big_dipper.vector_store_reader(
        load_dir_path="scripts/pickles",
        store_names=["facebook_dream_archetypes_store.dat"],
        use_cpu=int(os.getenv("USE_CPU", "1")),
    )
    
    results = __v__.vector_store["facebook_dream_archetypes_store"].similarity_search(dream_text)
    
    # Convert to the format expected by Chart.js
    counts = pd.Series([df.loc[_.metadata["row"]]["archetype"] for _ in results]).value_counts()
    
    # Convert NumPy types to native Python types
    data = {
        'labels': counts.index.tolist(),
        'values': [int(val) for val in counts.values.tolist()]
    }
    
    return jsonify(data)

@app.route('/get_rarity_score')
def get_rarity_score():
    score = calculate_rarity_score(selected_archetype)
    
    return jsonify({
        'score': score,
        'archetype': selected_archetype
    })

def generate_time_series_data():
    archetypes = ['explorer', 'everyman', 'hero', 'outlaw', 'sage']
    end_date = datetime.now()
    
    # Generate dates for the past 6 months
    dates = [(end_date - timedelta(days=i*30)).strftime('%Y-%m') for i in range(6)]
    dates.reverse()  # Chronological order
    
    data = []
    for archetype in archetypes:
        # Generate somewhat smooth trend with some randomness
        base_value = np.random.randint(5, 15)
        trend = np.random.choice([-1, 0, 1])  # Trend direction
        values = []
        
        for i in range(len(dates)):
            # Value changes with some trend and randomness
            val = max(1, base_value + trend * i + np.random.randint(-3, 4))
            # Convert NumPy int64 to regular Python int
            val = int(val)
            values.append(val)
        
        data.append({
            'archetype': archetype,
            'values': values
        })
    
    return {
        'dates': dates,
        'data': data
    }

@app.route('/get_time_series_data')
def get_time_series_data():
    time_data = generate_time_series_data()
    return jsonify(time_data)


@app.route("/get_chart_history", methods=["GET"])
def get_chart_history():
    charts = ChartData.query.order_by(ChartData.timestamp.desc()).all()
    return jsonify([{ "id": c.id, "chart_type": c.chart_type, "timestamp": c.timestamp.strftime("%Y-%m-%d %H:%M:%S") } for c in charts])

@app.route('/get_resources/<archetype>')
def get_resources(archetype):
    """
    Load resources for a specific archetype from text files.
    Each archetype should have a JSON file in the resources directory.
    Falls back to default resources if the file doesn't exist.
    """
    try:
        # Sanitize the archetype name to prevent directory traversal
        archetype = os.path.basename(archetype.lower())
        
        # Path to the archetype's resource file
        resource_file = os.path.join(RESOURCES_DIR, f"{archetype}.json")
        
        # Check if the file exists
        if os.path.exists(resource_file):
            with open(resource_file, 'r') as f:
                resources = json.load(f)
        else:
            # Load default resources if archetype-specific file doesn't exist
            default_file = os.path.join(RESOURCES_DIR, "default.json")
            with open(default_file, 'r') as f:
                resources = json.load(f)
        
        print("sent reading notes...")
        return jsonify(resources)
    
    except Exception as e:
        print(f"Error loading resources: {e}")
        # Return default resources as fallback
        default_resources = [
            {
                "title": "Understanding Jungian Archetypes",
                "description": "An introduction to Carl Jung's theory of archetypes and their significance in dream interpretation.",
                "links": [
                    {"type": "Article", "url": "https://conorneill.com/2018/04/21/understanding-personality-the-12-jungian-archetypes/"},
                ]
            },
            {
                "title": "Dream Symbolism Dictionary",
                "description": "Comprehensive guide to common dream symbols and their potential meanings across cultures.",
                "links": [
                    {"type": "Reference", "url": "https://www.dreamdictionary.org/"}
                ]
            }
        ]
        return jsonify(default_resources)

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    print("Starting server on localhost:8000, with Prometheus!")
    app.run(port=8000, debug=True)

