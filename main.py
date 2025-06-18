from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from TikTokApi import TikTokApi
import datetime
import asyncio
import logging
import re
from typing import Optional, List, Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TikTok Account Age Checker API")

# Pydantic models for response
class EstimateDetail(BaseModel):
    date: str
    confidence: int
    method: str

class EstimationDetails(BaseModel):
    all_estimates: List[EstimateDetail]
    note: str

class TikTokUserData(BaseModel):
    username: str
    nickname: str
    avatar: str
    followers: int
    total_likes: int
    verified: bool
    description: Optional[str]
    region: Optional[str]
    user_id: str
    estimated_creation_date: str
    account_age: str
    estimation_confidence: str
    estimation_method: str
    accuracy_range: str
    estimation_details: EstimationDetails

# TikTok Age Estimator class (ported from JavaScript)
class TikTokAgeEstimator:
    # User ID range estimation
    @staticmethod
    def estimate_from_user_id(user_id: str) -> Optional[datetime.datetime]:
        try:
            id_num = int(user_id)
            ranges = [
                {"min": 0, "max": 100000000, "date": datetime.datetime(2016, 9, 1)},
                {"min": 100000000, "max": 500000000, "date": datetime.datetime(2017, 1, 1)},
                {"min": 500000000, "max": 1000000000, "date": datetime.datetime(2017, 6, 1)},
                {"min": 1000000000, "max": 2000000000, "date": datetime.datetime(2018, 1, 1)},
                {"min": 2000000000, "max": 5000000000, "date": datetime.datetime(2018, 8, 1)},
                {"min": 5000000000, "max": 10000000000, "date": datetime.datetime(2019, 3, 1)},
                {"min": 10000000000, "max": 20000000000, "date": datetime.datetime(2019, 9, 1)},
                {"min": 20000000000, "max": 50000000000, "date": datetime.datetime(2020, 3, 1)},
                {"min": 50000000000, "max": 100000000000, "date": datetime.datetime(2020, 9, 1)},
                {"min": 100000000000, "max": 200000000000, "date": datetime.datetime(2021, 3, 1)},
                {"min": 200000000000, "max": 500000000000, "date": datetime.datetime(2021, 9, 1)},
                {"min": 500000000000, "max": 1000000000000, "date": datetime.datetime(2022, 3, 1)},
                {"min": 1000000000000, "max": 2000000000000, "date": datetime.datetime(2022, 9, 1)},
                {"min": 2000000000000, "max": 5000000000000, "date": datetime.datetime(2023, 3, 1)},
                {"min": 5000000000000, "max": 10000000000000, "date": datetime.datetime(2023, 9, 1)},
                {"min": 10000000000000, "max": 20000000000000, "date": datetime.datetime(2024, 3, 1)},
                {"min": 20000000000000, "max": 2**53, "date": datetime.datetime(2024, 9, 1)},
            ]
            for r in ranges:
                if r["min"] <= id_num < r["max"]:
                    return r["date"]
            return datetime.datetime.now()
        except (ValueError, TypeError):
            return None

    # Username pattern analysis
    @staticmethod
    def estimate_from_username(username: str) -> Optional[datetime.datetime]:
        if not username:
            return None
        patterns = [
            {"regex": r"^user\d{7,9}$", "date": datetime.datetime(2016, 9, 1)},
            {"regex": r"^[a-z]{3,8}\d{2,4}$", "date": datetime.datetime(2017, 3, 1)},
            {"regex": r"^\w{3,8}$", "date": datetime.datetime(2017, 9, 1)},
            {"regex": r"^.{1,8}$", "date": datetime.datetime(2018, 6, 1)},
        ]
        for pattern in patterns:
            if re.match(pattern["regex"], username):
                return pattern["date"]
        return None

    # Profile metrics analysis
    @staticmethod
    def estimate_from_metrics(followers: int, total_likes: int, verified: bool) -> Optional[datetime.datetime]:
        scores = []
        if followers > 1000000:
            scores.append(datetime.datetime(2018, 1, 1))
        elif followers > 100000:
            scores.append(datetime.datetime(2019, 1, 1))
        elif followers > 10000:
            scores.append(datetime.datetime(2020, 1, 1))
        else:
            scores.append(datetime.datetime(2021, 1, 1))
        
        if total_likes > 10000000:
            scores.append(datetime.datetime(2018, 6, 1))
        elif total_likes > 1000000:
            scores.append(datetime.datetime(2019, 6, 1))
        elif total_likes > 100000:
            scores.append(datetime.datetime(2020, 6, 1))
        
        if verified:
            scores.append(datetime.datetime(2018, 1, 1))
        
        if not scores:
            return None
        return min(scores)

    # Combined estimation with confidence scoring
    @staticmethod
    def estimate_account_age(user_id: str, username: str, followers: int = 0, total_likes: int = 0, verified: bool = False) -> Dict[str, Any]:
        estimates = []
        confidence = {"low": 1, "medium": 2, "high": 3}
        
        user_id_est = TikTokAgeEstimator.estimate_from_user_id(user_id)
        if user_id_est:
            estimates.append({
                "date": user_id_est,
                "confidence": confidence["high"],
                "method": "User ID Analysis"
            })
        
        username_est = TikTokAgeEstimator.estimate_from_username(username)
        if username_est:
            estimates.append({
                "date": username_est,
                "confidence": confidence["medium"],
                "method": "Username Pattern"
            })
        
        metrics_est = TikTokAgeEstimator.estimate_from_metrics(followers, total_likes, verified)
        if metrics_est:
            estimates.append({
                "date": metrics_est,
                "confidence": confidence["low"],
                "method": "Profile Metrics"
            })
        
        if not estimates:
            return {
                "estimated_date": datetime.datetime.now(),
                "confidence": "very_low",
                "method": "Default",
                "accuracy": "± 2 years",
                "all_estimates": []
            }
        
        weighted_sum = sum(est["date"].timestamp() * est["confidence"] for est in estimates)
        total_weight = sum(est["confidence"] for est in estimates)
        final_date = datetime.datetime.fromtimestamp(weighted_sum / total_weight)
        
        max_confidence = max(est["confidence"] for est in estimates)
        confidence_level = "high" if max_confidence == 3 else "medium" if max_confidence == 2 else "low"
        primary_method = next((est["method"] for est in estimates if est["confidence"] == max_confidence), "Combined")
        accuracy = "± 6 months" if confidence_level == "high" else "± 1 year" if confidence_level == "medium" else "± 2 years"
        
        return {
            "estimated_date": final_date,
            "confidence": confidence_level,
            "method": primary_method,
            "accuracy": accuracy,
            "all_estimates": estimates
        }

# Helper function to format date
def format_date(date: datetime.datetime) -> str:
    return date.strftime("%B %d, %Y")

# Helper function to calculate account age
def calculate_age(created_date: datetime.datetime) -> str:
    now = datetime.datetime.now()
    diff = now - created_date
    diff_days = diff.days
    diff_months = diff_days // 30
    diff_years = diff_months // 12
    
    if diff_years > 0:
        remaining_months = diff_months % 12
        years_str = f"{diff_years} year{'s' if diff_years > 1 else ''}"
        months_str = f" and {remaining_months} month{'s' if remaining_months > 1 else ''}" if remaining_months > 0 else ""
        return f"{years_str}{months_str}"
    elif diff_months > 0:
        return f"{diff_months} month{'s' if diff_months > 1 else ''}"
    else:
        return f"{diff_days} day{'s' if diff_days > 1 else ''}"

# Initialize TikTokApi
async def get_tiktok_client():
    try:
        verify_fp = "your_verify_fp_here"  # Replace with your verifyFp or use proxy
        client = await TikTokApi.get_instance(custom_verify_fp=verify_fp)
        return client
    except Exception as e:
        logger.error(f"Failed to initialize TikTokApi: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to initialize TikTok API client")

@app.get("/api/user/{username}", response_model=TikTokUserData)
async def get_user_data(username: str):
    try:
        client = await get_tiktok_client()
        user = await client.user(username=username).info()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Extract user details
        user_data = {
            "username": user.get("uniqueId", ""),
            "nickname": user.get("nickname", ""),
            "avatar": user.get("avatarLarger", ""),
            "followers": user.get("followerCount", 0),
            "total_likes": user.get("heartCount", 0),
            "verified": user.get("verified", False),
            "description": user.get("signature", ""),
            "region": user.get("region", ""),
            "user_id": user.get("id", "")
        }
        
        # Estimate account age
        age_estimate = TikTokAgeEstimator.estimate_account_age(
            user_data["user_id"],
            user_data["username"],
            user_data["followers"],
            user_data["total_likes"],
            user_data["verified"]
        )
        
        formatted_date = format_date(age_estimate["estimated_date"])
        account_age = calculate_age(age_estimate["estimated_date"])
        
        # Format estimation details
        estimation_details = {
            "all_estimates": [
                {
                    "date": format_date(est["date"]),
                    "confidence": est["confidence"],
                    "method": est["method"]
                } for est in age_estimate["all_estimates"]
            ],
            "note": "This is an estimated creation date based on available data. Actual creation date may vary."
        }
        
        return TikTokUserData(
            **user_data,
            estimated_creation_date=formatted_date,
            account_age=account_age,
            estimation_confidence=age_estimate["confidence"],
            estimation_method=age_estimate["method"],
            accuracy_range=age_estimate["accuracy"],
            estimation_details=estimation_details
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error processing request for {username}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching user data: {str(e)}")

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}
