# Personal Project Agent

A technical project manager with clean separation between backend API and frontend UI.

**Architecture**: FastAPI backend + Jinja2 templates + SQLite database

## Features

- **Clean API**: JSON REST endpoints at `/api` for programmatic access
- **Dashboard UI**: Browser-based dashboard showing today's focus
- **Room Dashboard**: Displays today's recommended project, next action, blockers, active projects
- **Projects**: Track multiple projects with notes, actions, blockers, and weekly goals
- **Extensible**: APIs ready for VS Code extension, CLI scripts, or other clients

## Files

- `api.py` - FastAPI backend with JSON API routes + HTML rendering
- `database.py` - SQLite database layer
- `templates/` - Jinja2 HTML templates
- `main.py` - CLI entry point (legacy, still works)
- `dashboard.py` - CLI dashboard display logic
- `menu.py` - CLI menu navigation
- `projects.db` - SQLite database (auto-created)
- `Dockerfile` - Docker image configuration
- `docker-compose.yml` - Docker Compose configuration
- `requirements.txt` - Python dependencies

## How to Run

### Prerequisites
- Python 3.7+ (for CLI)
- Docker & Docker Compose (for web app)
- No external dependencies for CLI (uses only standard library)

### Web App (FastAPI) - Quick Start

**Option 1: Docker (Recommended)**

1. Install Docker and Docker Compose
2. Navigate to project folder:
```powershell
cd c:\Users\curti\repos\agent_assistant
```

3. Start the app:
```powershell
docker-compose up
```

4. Open browser and go to:
```
http://localhost:8000
```

5. API endpoints available at `/api/*`

6. To stop:
```powershell
docker-compose down
```

### Deploy on Fly.io

This repo includes `fly.toml` for a Docker web service with a persistent Fly Volume. The volume keeps `projects.db` and uploaded recipe images across deploys/restarts.

1. Install the Fly CLI:
```powershell
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

2. Sign in:
```powershell
fly auth login
```

3. Create the app if it does not exist yet:
```powershell
fly apps create agent-assistant-dreaston
```

4. Create the persistent volume:
```powershell
fly volumes create agent_assistant_data --size 1 --region lax
```

5. Set the OpenAI key if you want the chat/review features available on the hosted app:
```powershell
fly secrets set OPENAI_API_KEY=your_key_here
```

6. Deploy:
```powershell
fly deploy
```

The deployed recipe upload page will be:

```text
https://agent-assistant-dreaston.fly.dev/apps/recipes/import?project_id=2&action_id=10
```

The deploy uses:

```text
DB_PATH=/data/projects.db
UPLOADS_DIR=/data/uploads
```

On first deploy, the app copies the bundled `projects.db` into `/data/projects.db` if the persistent database does not already exist.

**Option 2: Local Python**

1. Install dependencies:
```powershell
C:\Users\curti\AppData\Local\Programs\Python\Python311\python.exe -m pip install fastapi uvicorn jinja2
```

2. Run the app:
```powershell
C:\Users\curti\AppData\Local\Programs\Python\Python311\python.exe api.py
```

3. Open browser to `http://localhost:8000`

## API Endpoints

All endpoints return JSON and are ready for external clients:

### Dashboard
- `GET /api/dashboard` - Today's focus data

### Projects
- `GET /api/projects` - List all projects
- `GET /api/projects/{id}` - Get project details
- `POST /api/projects` - Create project

### Notes
- `GET /api/projects/{id}/notes` - List notes
- `POST /api/projects/{id}/notes` - Add note

### Actions
- `GET /api/projects/{id}/actions` - List actions
- `POST /api/projects/{id}/actions` - Add action

### Blockers
- `GET /api/projects/{id}/blockers` - List blockers
- `POST /api/projects/{id}/blockers` - Add blocker
- `DELETE /api/projects/{id}/blockers/{blocker_id}` - Delete blocker

### Goals
- `GET /api/projects/{id}/goals` - List goals
- `POST /api/projects/{id}/goals` - Add goal
- `POST /api/projects/{id}/goals/{goal_id}/complete` - Mark goal complete

### CLI App - Quick Start

1. Navigate to the project folder:
```powershell
cd c:\Users\curti\repos\agent_assistant
```

2. Run the application:
```powershell
C:\Users\curti\AppData\Local\Programs\Python\Python311\python.exe main.py
```

Or use the batch file:
```powershell
.\run.bat
```

3. Use the menu to manage projects, notes, blockers, actions, and goals.

## Sample Data

On first run, the database is populated with:
- **3 Projects**: EEG headband, Recipe display app, Calcium imaging analysis
- **Sample notes** for each project
- **Sample blockers** with severity levels
- **Sample recommended actions** with priority levels
- **Sample weekly goals**

## Web App Features

The FastAPI web app includes:

### Dashboard Page (/)
- Today's recommended project
- Next concrete action (highest priority)
- Summary metrics (projects, blockers, actions, goal progress)
- Active projects grid
- All blockers with severity
- All goals with completion status

### Projects Page (/projects)
- Create new projects
- View all projects
- Link to project details

### Project Detail Page (/projects/{id})
- View all project data (notes, actions, blockers, goals)
- Add notes, actions, blockers, and goals
- Mark goals as complete
- Remove blockers

## Database Schema

The SQLite database includes:
- `projects` - Project metadata
- `notes` - Project notes
- `blockers` - Project blockers with severity
- `recommended_actions` - Prioritized next actions
- `weekly_goals` - Weekly goals with completion status

## Example Usage

```
📊 PERSONAL PROJECT AGENT - DASHBOARD
================================================================================

🎯 ACTIVE PROJECTS
- EEG headband
- Recipe display app
- Calcium imaging analysis

⚡ RECOMMENDED NEXT ACTIONS
🔴 [EEG headband] Research EEG signal amplification circuits
🔴 [EEG headband] Set up development board
...

📝 PROJECT NOTES (Latest)
EEG headband:
  • Need to research BCI signal processing
  • Contact hardware supplier for quotes
...
```

## Customization

Edit `database.py` `populate_sample_data()` method to customize:
- Project names
- Initial notes, blockers, actions, and goals

Or use the web app/CLI to add/modify data after launching.

## Project Structure

```
agent_assistant/
├── app.py                  # Streamlit web application
├── main.py                 # CLI entry point
├── database.py             # SQLite database operations
├── dashboard.py            # Dashboard display logic (CLI)
├── menu.py                 # Interactive CLI menu
├── requirements.txt        # Python dependencies
├── Dockerfile              # Docker configuration
├── docker-compose.yml      # Docker Compose setup
├── .dockerignore            # Docker ignore file
├── projects.db             # SQLite database (auto-created)
└── README.md               # This file
```

## Technology Stack

- **Backend**: Python 3.11 + FastAPI 0.104
- **Server**: Uvicorn
- **Frontend**: Jinja2 templates + HTML/CSS
- **Database**: SQLite3
- **Containerization**: Docker & Docker Compose

## Architecture

```
FastAPI Backend (api.py)
├── /api/* JSON REST endpoints (no HTML)
├── / HTML routes (use Jinja2 templates)
├── Form submission routes (POST-Redirect-Get pattern)
└── Database layer (database.py)
    └── SQLite (projects.db)

Clients can connect to:
- Browser (renders HTML from / routes)
- External tools (call /api/* endpoints for JSON)
- VS Code extension (call /api/* endpoints)
- CLI scripts (call /api/* endpoints)
```

## Notes

- The web app and CLI share the same SQLite database (`projects.db`)
- Changes made in one interface are immediately visible in the other
- Database persists across container restarts when using Docker Compose
- No authentication required (MVP stage)
- Sample data is auto-populated on first run
