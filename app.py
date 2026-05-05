from flask import Flask, render_template, request, redirect, url_for, jsonify
import json
import os
import threading
import gspread
from google.oauth2.service_account import Credentials
import cloudinary
from generator import (
    run_full_generation, regenerate_sku, get_status, init_services
)

app = Flask(__name__)
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return None


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_sheet_data(config):
    """Helper to get worksheet and all records."""
    rules = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    login = Credentials.from_service_account_file(config["credentials_file"], scopes=rules)
    gspread_app = gspread.authorize(login)
    worksheet = gspread_app.open_by_key(config["sheet_id"]).sheet1
    all_info = worksheet.get_all_records()
    headers = worksheet.row_values(1)
    
    # Debug dump to see column names
    try:
        with open("debug_sheet.json", "w") as f:
            json.dump({"headers": headers, "sample": all_info[0] if all_info else {}}, f, indent=2)
    except:
        pass
        
    return worksheet, all_info, headers


@app.route("/")
def index():
    config = load_config()
    if not config:
        return redirect(url_for("setup"))
    return redirect(url_for("gallery"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    config = load_config() or {
        "replicate_token": "",
        "cloudinary_cloud_name": "",
        "cloudinary_api_key": "",
        "cloudinary_api_secret": "",
        "sheet_id": "",
        "credentials_file": "/Users/menon/dev/ImageGenerator/credentials.json",
    }
    error = None
    success = False

    if request.method == "POST":
        config = {
            "replicate_token": request.form.get("replicate_token", "").strip(),
            "cloudinary_cloud_name": request.form.get("cloudinary_cloud_name", "").strip(),
            "cloudinary_api_key": request.form.get("cloudinary_api_key", "").strip(),
            "cloudinary_api_secret": request.form.get("cloudinary_api_secret", "").strip(),
            "sheet_id": request.form.get("sheet_id", "").strip(),
            "credentials_file": request.form.get("credentials_file", "").strip(),
        }
        # Validate
        missing = [k for k, v in config.items() if not v]
        if missing:
            error = f"Missing fields: {', '.join(missing)}"
        else:
            save_config(config)
            success = True

    return render_template("setup.html", config=config, error=error, success=success)


@app.route("/gallery")
def gallery():
    config = load_config()
    if not config:
        return redirect(url_for("setup"))

    status_filter = request.args.get("filter", "all")
    products = []
    error = None

    try:
        worksheet, all_info, headers = get_sheet_data(config)
        status_col_name = "approved/rejected images"
        cloud_name = config["cloudinary_cloud_name"]

        for row_idx, row in enumerate(all_info, start=2):
            sku = str(row.get("SKU", "")).strip()
            colour = str(row.get("Variant Color", "")).strip()
            product_name = str(row.get("Product", "")).strip()

            if not sku and not product_name and not colour:
                continue

            status = str(row.get(status_col_name, "")).strip()
            display_status = status if status else "Pending Review"
            
            if not sku:
                display_status = "Missing SKU"

            # Apply filter
            sl = status.lower()
            if status_filter == "approved" and sl != "approved":
                continue
            elif status_filter == "rejected" and sl not in ["rejected", "regenerating"]:
                continue
            elif status_filter == "pending" and sl in ["approved", "rejected"]:
                continue

            products.append({
                "sku": sku,
                "row_idx": row_idx,
                "status": display_status,
                "final_url": f"https://res.cloudinary.com/{cloud_name}/image/upload/{sku}_final.jpg",
                "final2_url": f"https://res.cloudinary.com/{cloud_name}/image/upload/{sku}_final2.jpg",
                "step2_url": f"https://res.cloudinary.com/{cloud_name}/image/upload/{sku}_step2.jpg",
                "flatlay_url": row.get("Front Flatlay Image URL", ""),
                "colour": colour,
                "product_name": product_name,
                "improve_prompt": str(row.get("improvement prompt", "")).strip(),
            })
    except Exception as e:
        error = str(e)

    gen_status = get_status()
    return render_template("gallery.html", products=products,
                           status_filter=status_filter, gen_status=gen_status, error=error)


@app.route("/api/approve/<sku>", methods=["POST"])
def approve(sku):
    config = load_config()
    try:
        worksheet, all_info, headers = get_sheet_data(config)
        status_col = headers.index("approved/rejected images") + 1
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                worksheet.update_cell(idx, status_col, "Approved")
                return jsonify({"success": True, "status": "Approved"})
        return jsonify({"success": False, "error": "SKU not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reject/<sku>", methods=["POST"])
def reject(sku):
    config = load_config()
    try:
        worksheet, all_info, headers = get_sheet_data(config)
        status_col = headers.index("approved/rejected images") + 1
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                worksheet.update_cell(idx, status_col, "Rejected")
                break
        return jsonify({"success": True, "status": "Rejected"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/improve/<sku>", methods=["POST"])
def improve(sku):
    data = request.json
    prompt = data.get("prompt", "")
    config = load_config()
    from generator import improve_sku
    try:
        worksheet, all_info, headers = get_sheet_data(config)
        status_col = headers.index("approved/rejected images") + 1
        prompt_col = headers.index("improvement prompt") + 1 if "improvement prompt" in headers else None
        
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                worksheet.update_cell(idx, status_col, "Regenerating")
                if prompt_col:
                    worksheet.update_cell(idx, prompt_col, prompt)
                break
                
        thread = threading.Thread(target=improve_sku, args=(config, sku), daemon=True)
        thread.start()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/regenerate/<sku>", methods=["POST"])
def regenerate(sku):
    config = load_config()
    from generator import regenerate_sku
    try:
        worksheet, all_info, headers = get_sheet_data(config)
        status_col = headers.index("approved/rejected images") + 1
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                worksheet.update_cell(idx, status_col, "Regenerating")
                break
                
        thread = threading.Thread(target=regenerate_sku, args=(config, sku), daemon=True)
        thread.start()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/run_group", methods=["POST"])
def run_group():
    data = request.json
    group_name = data.get("group_name")
    if not group_name:
         return jsonify({"error": "Group name required"}), 400
         
    from generator import run_full_generation, get_status
    status = get_status()
    if status["running"]:
        return jsonify({"error": "Generation already running"}), 400
        
    config = load_config()
    thread = threading.Thread(target=run_full_generation, args=(config, group_name))
    thread.daemon = True
    thread.start()
    return jsonify({"success": True})

@app.route("/api/run", methods=["POST"])
def run_generation():
    config = load_config()
    status = get_status()
    if status["running"]:
        return jsonify({"success": False, "error": "Generation already running"}), 409
    thread = threading.Thread(target=run_full_generation, args=(config,), daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Generation started"})


@app.route("/api/stop", methods=["POST"])
def stop_generation():
    from generator import update_status
    update_status(stop_requested=True)
    return jsonify({"success": True})


@app.route("/api/status")
def status():
    return jsonify(get_status())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n✨ Monks Image Curator starting...")
    print(f"🌐 Open http://127.0.0.1:{port} in your browser\n")
    app.run(host="127.0.0.1", port=port)
