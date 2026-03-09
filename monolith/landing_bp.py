import logging
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt

landing_bp = Blueprint('landing_bp', __name__)

@landing_bp.route('/')
@jwt_required(optional=True)
def landing_page():
    user_email = None
    user_name = None
    is_admin = False
    
    try:
        claims = get_jwt()
        if claims:
            user_email = claims.get('sub')
            user_name = claims.get('name', user_email)
            is_admin = claims.get('is_admin', False)
        else:
            current_app.logger.info("No valid JWT claims found; user not logged in.")
    except Exception as e:
        current_app.logger.warning(f"Could not get JWT identity: {e}")

    return render_template(
        'landing.html',
        user_email=user_email,
        user_name=user_name,
        is_admin=is_admin,
        # Point to internal monolith routes, not external service URLs
        FORMS_SERVICE_URL='/forms',
        LOGIN_SERVICE_URL='/',
        DASHBOARD_SERVICE_URL='/dashboard',
        VIEWER_SERVICE_URL='/viewer',
    )

@landing_bp.route('/user_info', methods=['GET'])
@jwt_required()
def user_info():
    try:
        claims = get_jwt()
        user_email = claims.get('sub')
        user_name = claims.get('name', user_email)
        is_admin = claims.get('is_admin', False)
        
        if user_email:
            current_app.logger.info(f"User info requested for: {user_email} (admin: {is_admin})")
            return jsonify({
                "email": user_email,
                "name": user_name,
                "is_admin": is_admin,
                "roles": ["admin"] if is_admin else ["user"]
            }), 200
        return jsonify({"msg": "Unauthorized: No valid user identity"}), 401
    except Exception as e:
        current_app.logger.error(f"Error fetching user info: {e}", exc_info=True)
        return jsonify({"msg": "Internal server error"}), 500
