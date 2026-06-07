#!/usr/bin/env python3
"""
Personal Project Agent - Technical Project Manager
A minimal MVP dashboard for managing multiple projects with notes, blockers, and goals.
"""

import sqlite3
import os
from datetime import datetime
from database import Database
from dashboard import Dashboard
from menu import Menu

DB_PATH = "projects.db"


def main():
    """Main entry point."""
    db = Database(DB_PATH)
    db.init()
    
    # If database is empty, populate with sample data
    if db.get_project_count() == 0:
        db.populate_sample_data()
    
    dashboard = Dashboard(db)
    menu = Menu(db, dashboard)
    
    while True:
        dashboard.show_main_dashboard()
        menu.show_main_menu()


if __name__ == "__main__":
    main()
