    # app.py
    import os
    import time
    from flask import Flask, render_template, jsonify, redirect, url_for, flash, request
    from flask_jwt_extended import (
        JWTManager, jwt_required, get_jwt_identity, unset_jwt_cookies
    )
    from datetime import datetime

    app = Flask(__name__)

    # --- Flask Config ---
    # Ensure these match your login service for JWT to work correctly across services
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-form-viewer')
    app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'dev-jwt') # Must match login service
    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_COOKIE_SECURE'] = True # Set to True in production over HTTPS
    app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
    # Important: JWT_COOKIE_DOMAIN should be the common base domain if services are on different subdomains
    # e.g., '.run.app' or your custom domain like '.yourdomain.com'
    app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')
    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'http://localhost:8080/login') # URL of your login service

    # --- Extensions ---
    jwt = JWTManager(app)

    # --- Mock Data for Forms ---
    # This data would typically come from your 'forms' microservice or a database
    MOCK_FORMS = [
        {
            'id': 'form-101',
            'title': 'Employee Onboarding Checklist',
            'dateSubmitted': '2025-06-28',
            'submittedBy': 'Alice Johnson',
            'data': {
                'Employee Name': 'John Doe',
                'Department': 'Engineering',
                'Start Date': '2025-07-01',
                'Manager': 'Jane Smith',
                'IT Setup Completed': 'Yes',
                'HR Paperwork Submitted': 'Yes',
                'Welcome Kit Issued': 'Yes',
                'Notes': 'John is a software engineer. Needs access to project Alpha and Beta.'
            }
        },
        {
            'id': 'form-102',
            'title': 'Expense Reimbursement Request',
            'dateSubmitted': '2025-06-29',
            'submittedBy': 'Bob Williams',
            'data': {
                'Employee Name': 'Bob Williams',
                'Date of Expense': '2025-06-25',
                'Category': 'Travel',
                'Amount': '$150.00',
                'Description': 'Flight ticket for client meeting in New York.',
                'Receipt Attached': 'Yes',
                'Approval Status': 'Pending'
            }
        },
        {
            'id': 'form-103',
            'title': 'Leave Request Form',
            'dateSubmitted': '2025-06-30',
            'submittedBy': 'Charlie Brown',
            'data': {
                'Employee Name': 'Charlie Brown',
                'Leave Type': 'Vacation',
                'Start Date': '2025-07-15',
                'End Date': '2025-07-22',
                'Total Days': '8',
                'Reason': 'Family trip to the mountains.',
                'Approved By': 'N/A'
            }
        },
        {
            'id': 'form-104',
            'title': 'IT Support Ticket',
            'dateSubmitted': '2025-07-01',
            'submittedBy': 'Diana Prince',
            'data': {
                'Requester Name': 'Diana Prince',
                'Issue Type': 'Hardware',
                'Device': 'Laptop',
                'Description': 'Laptop screen flickering intermittently. Needs repair or replacement.',
                'Urgency': 'High',
                'Status': 'Open'
            }
        }
    ]

    # --- JWT Error Handling ---
    @jwt.unauthorized_loader
    @jwt.invalid_token_loader
    @jwt.expired_token_loader
    def token_error_response(callback):
        """
        Handles JWT errors by redirecting to the login service.
        """
        flash('Su sesión ha caducado o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
        # Redirect to the login service URL
        return redirect(app.config['LOGIN_SERVICE_URL'])

    # --- Routes ---
    @app.route('/')
    @jwt_required() # Protect this route with JWT
    def index():
        """
        Renders the main HTML page for the form viewer.
        Requires a valid JWT token.
        """
        current_user_identity = get_jwt_identity() # Get user identity from JWT
        app.logger.info(f"User {current_user_identity} accessed FormViewerService.")
        
        # Simulate a network delay for fetching data
        time.sleep(0.5)
        return render_template('index.html', forms=MOCK_FORMS, current_user=current_user_identity)

    @app.route('/logout')
    def logout():
        """
        Logs out the user by unsetting JWT cookies and redirecting to the login service.
        """
        response = redirect(app.config['LOGIN_SERVICE_URL'])
        unset_jwt_cookies(response)
        flash('Sesión cerrada.', 'info')
        return response

    @app.route('/api/forms')
    @jwt_required() # Protect API endpoint as well
    def get_forms_api():
        """
        Provides a JSON API endpoint for the form data.
        Requires a valid JWT token.
        """
        time.sleep(0.1) # Simulate a small API delay
        return jsonify(MOCK_FORMS)

    @app.route('/api/forms/<string:form_id>')
    @jwt_required() # Protect API endpoint as well
    def get_form_by_id_api(form_id):
        """
        Provides a JSON API endpoint for a single form by its ID.
        Requires a valid JWT token.
        """
        time.sleep(0.1) # Simulate a small API delay
        form = next((f for f in MOCK_FORMS if f['id'] == form_id), None)
        if form:
            return jsonify(form)
        return jsonify({'error': 'Form not found'}), 404

    # --- Health Check Route ---
    @app.route('/health')
    def health_check():
        """Health check endpoint for Cloud Run"""
        health_status = {
            'status': 'healthy',
            'service': 'form-viewer-service',
            'timestamp': datetime.now().isoformat()
        }
        status_code = 200
        return health_status, status_code

    # Add a startup check route
    @app.route('/startup')
    def startup_check():
        """Startup check endpoint for Cloud Run"""
        return {
            'status': 'ready',
            'service': 'form-viewer-service',
            'port': os.environ.get('PORT', '8080'),
            'timestamp': datetime.now().isoformat()
        }, 200

    # --- Run App ---
    if __name__ == '__main__':
        port = int(os.environ.get('PORT', 8080))
        debug_mode = os.environ.get('FLASK_ENV') == 'development'

        print(f"Starting Flask app on port {port}")
        print(f"Debug mode: {debug_mode}")
        print(f"JWT Cookie Domain: {app.config['JWT_COOKIE_DOMAIN']}")
        print(f"Login Service URL: {app.config['LOGIN_SERVICE_URL']}")

        try:
            app.run(
                debug=debug_mode,
                host='0.0.0.0',
                port=port,
                threaded=True,
                use_reloader=False # Important: disable reloader in production
            )
        except Exception as e:
            print(f"Error starting Flask app: {e}")
            raise

    