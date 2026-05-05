import replicate
import gspread
import time
import random
import requests
import io
import json
import os
import threading
from PIL import Image
from google.oauth2.service_account import Credentials
import cloudinary
import cloudinary.uploader

MAX_SIZE_BYTES = 20 * 1024 * 1024

# Thread-safe generation status
generation_status = {
    "running": False,
    "current_sku": None,
    "current_step": None,
    "sku_steps": {},
    "total_products": 0,
    "completed_products": 0,
    "errors": [],
    "credits_error": False,
    "credits_error_message": "",
    "rate_limit_error": False,
    "rate_limit_message": "",
    "stop_requested": False,
    "last_saved_pic": None,
    "log": []
}
_status_lock = threading.Lock()

def update_status(sku=None, **kwargs):
    with _status_lock:
        if sku and "current_step" in kwargs:
            generation_status.setdefault("sku_steps", {})[sku] = kwargs["current_step"]
        if sku and kwargs.get("current_step") is None and "current_step" in kwargs:
            if sku in generation_status.setdefault("sku_steps", {}):
                del generation_status["sku_steps"][sku]
        generation_status.update(kwargs)

def add_log(message):
    with _status_lock:
        generation_status["log"].append(message)
        if len(generation_status["log"]) > 100:
            generation_status["log"] = generation_status["log"][-100:]
    print(message)

def get_status():
    with _status_lock:
        return dict(generation_status)

def compress_and_upload(image_url, public_id):
    response = requests.get(image_url)
    image_data = response.content
    if len(image_data) <= MAX_SIZE_BYTES:
        return cloudinary.uploader.upload(image_url, public_id=public_id, overwrite=True)
    img = Image.open(io.BytesIO(image_data))
    if img.mode == 'RGBA':
        img = img.convert('RGB')
    quality = 90
    while quality >= 10:
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=quality)
        size = buffer.tell()
        if size <= MAX_SIZE_BYTES:
            buffer.seek(0)
            return cloudinary.uploader.upload(buffer, public_id=public_id, overwrite=True)
        quality -= 10
    buffer.seek(0)
    return cloudinary.uploader.upload(buffer, public_id=public_id, overwrite=True)

def init_services(config):
    """Initialize Cloudinary, Google Sheets, and Replicate from config."""
    cloudinary.config(
        cloud_name=config["cloudinary_cloud_name"],
        api_key=config["cloudinary_api_key"],
        api_secret=config["cloudinary_api_secret"]
    )
    rules = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    login = Credentials.from_service_account_file(config["credentials_file"], scopes=rules)
    gspread_app = gspread.authorize(login)
    worksheet = gspread_app.open_by_key(config["sheet_id"]).sheet1
    ai = replicate.Client(api_token=config["replicate_token"])
    return gspread_app, worksheet, ai

_cached_styling_images = None

def get_random_styling_image(gspread_app):
    global _cached_styling_images
    if _cached_styling_images is None:
        try:
            styling_sheet_id = "1_ECgt2aozREcpcFEw2ib_igMtIE63gA7bTmC2R0kP7E"
            sheet = gspread_app.open_by_key(styling_sheet_id).sheet1
            col_data = sheet.col_values(1)
            _cached_styling_images = [url for url in col_data if url and "http" in str(url)]
        except Exception:
            _cached_styling_images = []
    if _cached_styling_images:
        return random.choice(_cached_styling_images)
    return None

def _check_credits_error(e):
    """Check if an exception is a credits/billing or rate limit error."""
    msg = str(e).lower()
    
    if "429" in msg or "rate limit" in msg:
        update_status(rate_limit_error=True, rate_limit_message=str(e))
        return True
        
    if "402" in msg or "billing" in msg or "insufficient" in msg or "credit" in msg or "payment" in msg:
        update_status(credits_error=True, credits_error_message=str(e))
        return True
    return False

def generate_for_sku(ai, sku, row_data, styling_url):
    """Run the full 4-step generation pipeline for a single SKU."""
    flatlay_front = row_data.get("Front Flatlay Image URL", "")
    model_url = row_data.get("Model Image URL", "")
    extra_note = row_data.get("Special Prompt", "")
    extra_note2 = row_data.get("Special Prompt 2", "")
    saved_pic = ""
    update_status(sku=sku, last_saved_pic=None)

    def check_stop():
        if generation_status.get("stop_requested"):
            raise Exception("Manually stopped by user")

    check_stop()

    # Step 1
    update_status(sku=sku, current_step=1)
    add_log(f"[{sku}] Step 1: Draping clothes...")
    time.sleep(11)
    check_stop()
    try:
        result = ai.run("google/nano-banana-pro", input={
            "prompt": "drape the clothes from the flatlay onto the person in the styling image.",
            "image_input": [flatlay_front, styling_url],
            "aspect_ratio": "4:5", "resolution": "4K"
        })
        saved_pic = str(result)
        update_status(sku=sku, last_saved_pic=saved_pic)
        add_log(f"[{sku}] Step 1 complete")
    except Exception as e:
        _check_credits_error(e)
        raise

    check_stop()

    # Step 2
    update_status(sku=sku, current_step=2)
    add_log(f"[{sku}] Step 2: Face transfer...")
    time.sleep(11)
    check_stop()
    try:
        result = ai.run("google/nano-banana-pro", input={
            "prompt": "transfer the face from image 1 to image 2 exactly without changing image 2's pose",
            "image_input": [model_url, saved_pic],
            "aspect_ratio": "4:5", "resolution": "4K"
        })
        saved_pic = str(result)
        update_status(sku=sku, last_saved_pic=saved_pic)
        compress_and_upload(saved_pic, f"{sku}_step2")
        add_log(f"[{sku}] Step 2 complete")
    except Exception as e:
        _check_credits_error(e)
        raise

    check_stop()

    # Step 3
    update_status(sku=sku, current_step=3)
    add_log(f"[{sku}] Step 3: Special prompt...")
    time.sleep(11)
    check_stop()
    if extra_note:
        try:
            result = ai.run("google/nano-banana-pro", input={
                "prompt": str(extra_note), "image_input": [saved_pic],
                "aspect_ratio": "4:5", "resolution": "4K"
            })
            saved_pic = str(result)
            update_status(sku=sku, last_saved_pic=saved_pic)
            add_log(f"[{sku}] Step 3 complete")
        except Exception as e:
            _check_credits_error(e)
            raise
    compress_and_upload(saved_pic, f"{sku}_final")
    add_log(f"[{sku}] Uploaded final image")

    check_stop()

    # Step 4
    update_status(sku=sku, current_step=4)
    add_log(f"[{sku}] Step 4: Special prompt 2...")
    time.sleep(11)
    check_stop()
    if extra_note2:
        try:
            result = ai.run("google/nano-banana-pro", input={
                "prompt": str(extra_note2), "image_input": [saved_pic],
                "aspect_ratio": "4:5", "resolution": "4K"
            })
            saved_pic = str(result)
            update_status(sku=sku, last_saved_pic=saved_pic)
            add_log(f"[{sku}] Step 4 complete")
        except Exception as e:
            _check_credits_error(e)
            raise
    compress_and_upload(saved_pic, f"{sku}_final2")
    
    # Step 5: Improvement Pass
    improve_prompt = str(row_data.get("improvement prompt", "")).strip()
    
    if improve_prompt:
        update_status(sku=sku, current_step=5)
        add_log(f"[{sku}] Step 5: Improvement prompt...")
        time.sleep(11)
        check_stop()
        try:
            result = ai.run("google/nano-banana-pro", input={
                "prompt": improve_prompt, "image_input": [saved_pic],
                "aspect_ratio": "4:5", "resolution": "4K"
            })
            saved_pic = str(result)
            update_status(sku=sku, last_saved_pic=saved_pic)
            add_log(f"[{sku}] Step 5 complete")
        except Exception as e:
            _check_credits_error(e)
            raise
        compress_and_upload(saved_pic, f"{sku}_final")
    add_log(f"[{sku}] Generation complete!")
    return {"success": True}

def save_exception_state(worksheet, headers, row_idx, sku, step, product_num):
    try:
        last_pass_col = next((i + 1 for i, h in enumerate(headers) if h.lower() == "last succesful pass"), None)
        product_num_col = next((i + 1 for i, h in enumerate(headers) if h.lower() == "product number"), None)
        pass_num_col = next((i + 1 for i, h in enumerate(headers) if h.lower() == "pass number"), None)
        part_gen_col = next((i + 1 for i, h in enumerate(headers) if h.lower() == "partially generated"), None)

        if product_num_col:
            worksheet.update_cell(row_idx, product_num_col, product_num)
        if pass_num_col and step is not None:
            worksheet.update_cell(row_idx, pass_num_col, step)
        if part_gen_col and step is not None:
            worksheet.update_cell(row_idx, part_gen_col, step)

        last_pic = generation_status.get("last_saved_pic")
        if last_pass_col and last_pic and step is not None:
            try:
                res = compress_and_upload(last_pic, f"last pass/{sku} last pass")
                last_pass_url = res.get("secure_url", "")
                worksheet.update_cell(row_idx, last_pass_col, f"{step - 1}|{last_pass_url}")
            except Exception as upload_err:
                add_log(f"Failed to upload last pass for {sku}: {upload_err}")
    except Exception as e:
        add_log(f"Failed to write exception state to sheet: {e}")

def run_full_generation(config, target_product_name=None):
    """Run generation for all SKUs in the sheet or a specific product group."""
    update_status(running=True, current_sku=None, current_step=None,
                  total_products=0, completed_products=0, errors=[],
                  credits_error=False, credits_error_message="", stop_requested=False, log=[])
    try:
        gspread_app, worksheet, ai = init_services(config)
        all_info = worksheet.get_all_records()
        
        # Calculate total products to process
        total = 0
        for r in all_info:
            if not r.get("SKU"): continue
            if target_product_name:
                pname = str(r.get("Product Name", r.get("Product", ""))).strip()
                if pname != target_product_name: continue
            total += 1
            
        update_status(total_products=total)
        add_log(f"Starting generation for {total} products" + (f" in group '{target_product_name}'" if target_product_name else ""))
        headers = worksheet.row_values(1)
        
        for row_idx, row in enumerate(all_info, start=2):
            if not row.get("SKU"):
                break
            if target_product_name:
                pname = str(row.get("Product Name", row.get("Product", ""))).strip()
                if pname != target_product_name:
                    continue
                    
            if generation_status.get("credits_error"):
                add_log("Credits exhausted. Stopping.")
                break
            if generation_status.get("stop_requested"):
                add_log("Manually stopped by user.")
                break
                
            status_col_name = "approved/rejected images"
            current_status = str(row.get(status_col_name, "")).strip().lower()
            if current_status == "approved":
                continue

            sku = str(row["SKU"]).strip()
            update_status(current_sku=sku)
            styling = get_random_styling_image(gspread_app)
            if not styling or not row.get("Front Flatlay Image URL"):
                add_log(f"Skipping {sku} - missing images")
                continue
            try:
                generate_for_sku(ai, sku, row, styling)
                update_status(completed_products=generation_status["completed_products"] + 1)
            except Exception as e:
                with _status_lock:
                    generation_status["errors"].append(f"{sku}: {str(e)}")
                add_log(f"Error on SKU {sku}: {e}. Saving state...")
                save_exception_state(worksheet, headers, row_idx, sku, generation_status.get("current_step"), row_idx - 1)
                
                if generation_status.get("credits_error") or generation_status.get("stop_requested"):
                    break
        add_log("Generation run finished.")
    except Exception as e:
        add_log(f"Fatal error: {str(e)}")
    finally:
        update_status(running=False, current_sku=None, current_step=None)

def regenerate_sku(config, sku):
    """Regenerate a single SKU in the background."""
    try:
        gspread_app, worksheet, ai = init_services(config)
        all_info = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        status_col = headers.index("approved/rejected images") + 1
        row_data = None
        row_idx = None
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                row_data = row
                row_idx = idx
                break
        if not row_data:
            add_log(f"SKU {sku} not found")
            return
        styling = get_random_styling_image(gspread_app)
        if not styling:
            add_log(f"No styling images for {sku}")
            return
        update_status(current_sku=sku)
        add_log(f"Regenerating {sku}...")
        generate_for_sku(ai, sku, row_data, styling)
        worksheet.update_cell(row_idx, status_col, "Pending Review")
        add_log(f"Regeneration complete for {sku}")
    except Exception as e:
        add_log(f"Regeneration error for {sku}: {str(e)}. Saving state...")
        if row_idx is not None and headers is not None:
            save_exception_state(worksheet, headers, row_idx, sku, generation_status.get("current_step"), row_idx - 1)
    finally:
        update_status(sku=sku, current_sku=None, current_step=None)

def improve_sku(config, sku):
    """Run ONLY the 5th (improvement) pass on an existing final image."""
    try:
        gspread_app, worksheet, ai = init_services(config)
        all_info = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        status_col = headers.index("approved/rejected images") + 1
        row_data = None
        row_idx = None
        for idx, row in enumerate(all_info, start=2):
            if str(row.get("SKU", "")).strip() == sku:
                row_data = row
                row_idx = idx
                break
        
        if not row_data:
            add_log(f"SKU {sku} not found")
            return
            
        improve_prompt = str(row_data.get("improvement prompt", "")).strip()
        if not improve_prompt:
            add_log(f"No improvement prompt found for {sku}")
            return
            
        update_status(sku=sku, current_sku=sku, current_step=5)
        add_log(f"[{sku}] Running Improvement pass ONLY...")
        
        # Use the existing final image as input
        cloud_name = config["cloudinary_cloud_name"]
        input_image_url = f"https://res.cloudinary.com/{cloud_name}/image/upload/{sku}_final.jpg"
        
        try:
            result = ai.run("google/nano-banana-pro", input={
                "prompt": improve_prompt, 
                "image_input": [input_image_url],
                "aspect_ratio": "4:5", 
                "resolution": "4K"
            })
            saved_pic = str(result)
            update_status(last_saved_pic=saved_pic)
            
            # Overwrite the final image so the gallery picks it up
            compress_and_upload(saved_pic, f"{sku}_final")
            
            # Reset status to pending review so it can be approved
            worksheet.update_cell(row_idx, status_col, "Pending Review")
            add_log(f"[{sku}] Improvement complete!")
            
        except Exception as e:
            _check_credits_error(e)
            raise
            
    except Exception as e:
        add_log(f"Improvement error for {sku}: {str(e)}")
    finally:
        update_status(sku=sku, current_sku=None, current_step=None)
