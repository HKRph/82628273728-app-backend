import logging, json, uvicorn, os, base64, random
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import date, timedelta, datetime
from typing import Optional, Tuple, List
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, InputFile
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, ForeignKey, Text, Date, DateTime, Boolean, or_
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# --- Configuration ---
# *** IMPORTANT: REPLACE THESE WITH YOUR ACTUAL VALUES ***
BOT_TOKEN = "8085121840:AAHGpim6s0j8FU8yZ5jSiyu6Ol51Rdgod8E"  # Get this from @BotFather
BOT_USERNAME = "XeweeBot"             # Your bot's @username (without the @)
ADMIN_CHAT_IDS: Tuple[int, ...] = (7588209802, 6780778947) # Your Super Admin Telegram User IDs (can be multiple)
MINI_APP_URL = "https://82628273728-app-frontend-seven.vercel.app" # Your Vercel frontend URL - *** IMPORTANT: REPLACE WITH YOUR ACTUAL FRONTEND URL! ***
# ------------------------------------

# --- Xewee Feature Constants ---
REFERRAL_COMMISSION_PERCENT = 0.10  # 10% commission on referred user's task reward

MIN_WITHDRAWAL = 300.0              # Minimum amount a user can withdraw
MAX_WITHDRAWAL = 30000.0            # Maximum amount a user can withdraw
WITHDRAWAL_FEE_PERCENT = 0.03       # 3% fee on withdrawals

DAILY_BONUS = 10.0                  # Amount for daily login bonus
DAILY_BONUS_INVITE_REQ = 2          # Number of new invites required since last claim for daily bonus

TASK_MILESTONES = {"10_tasks": 50.0, "20_tasks": 150.0, "30_tasks": 400.0} # Bonus rewards for completing tasks

GIFT_TICKET_PRICE = 10.0            # Price per Gift Ticket
GIFT_MIN_AMOUNT = 30.0              # Minimum amount a user can gift
GIFT_MAX_AMOUNT = 80000.0
GIFT_FEE_PERCENT = 0.05             # 5% fee on gifted amount

GAME_ROOM_INACTIVITY_TIMEOUT_MIN = 60 # Minutes before an unjoined or stagnant game room is cancelled
GAME_TARGET_SCORE = 3 # First player to reach this score wins

# --- Database Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, autoincrement=False)
    first_name = Column(String, nullable=True) # Storing first name for better messages
    last_name = Column(String, nullable=True) # Added for more complete user info
    username = Column(String, nullable=True) # Added to easily retrieve for games/notifications
    balance = Column(Float, default=0.0)
    gift_tickets = Column(Integer, default=0)
    referral_count = Column(Integer, default=0) # Total referred users
    successful_referrals = Column(Integer, default=0) # Referred users who completed at least one task
    tasks_completed = Column(Integer, default=0) # Total tasks completed by user
    completed_task_ids = Column(Text, default="[]") # JSON list of task IDs completed by this user
    referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    status = Column(String, default="active") # active, banned, restricted
    status_until = Column(Date, nullable=True) # For temporary restrictions
    last_login_date = Column(Date, nullable=True) # For daily bonus claim
    daily_claim_invites = Column(Integer, default=0) # Invites since last daily claim
    claimed_milestones = Column(Text, default="{}") # JSON string for task milestones claimed
    
    created_game_rooms = relationship("GameRoom", foreign_keys="[GameRoom.creator_id]", back_populates="creator")
    joined_game_rooms = relationship("GameRoom", foreign_keys="[GameRoom.opponent_id]", back_populates="opponent")
    # No back_populates for winner to avoid circular references if not strictly needed in User model


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    description = Column(String)
    link = Column(String)
    reward = Column(Float)

class TaskSubmission(Base):
    __tablename__ = "task_submissions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    task_id = Column(Integer)
    text_proof = Column(Text)
    photo_proof_base64 = Column(Text)
    status = Column(String, default="pending") # pending, approved, rejected
    rejection_reason = Column(Text, nullable=True) # Added for more detail
    created_at = Column(Date, default=date.today())
    is_read = Column(Boolean, default=False) # For new notification system

class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    amount = Column(Float)
    fee = Column(Float, default=0.0)
    method = Column(String)
    details = Column(String)
    status = Column(String, default="pending") # pending, approved, rejected
    rejection_reason = Column(Text, nullable=True) # Added for more detail
    created_at = Column(Date, default=date.today())
    is_read = Column(Boolean, default=False) # For new notification system

class RedeemCode(Base):
    __tablename__ = "redeem_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)
    reward = Column(Float)
    uses_left = Column(Integer, default=1) # -1 for unlimited

class SystemInfo(Base):
    __tablename__ = "system_info"
    key = Column(String, primary_key=True)
    value = Column(String)

class UserEvent(Base): # For generic notifications like referrals, gifts
    __tablename__ = "user_events"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    event_type = Column(String) # e.g., 'referral_join', 'gift_received', 'game_won', 'game_lost', 'rain_prize_won'
    message = Column(Text)
    related_id = Column(BigInteger, nullable=True) # e.g., referrer_id, sender_id, game_id
    amount = Column(Float, nullable=True) # e.g., gift amount, commission amount
    created_at = Column(Date, default=date.today())
    is_read = Column(Boolean, default=False)

class GameRoom(Base):
    __tablename__ = "game_rooms"
    id = Column(Integer, primary_key=True)
    room_name = Column(String, nullable=True)
    creator_id = Column(BigInteger, ForeignKey("users.id"))
    opponent_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    bet_amount = Column(Float)
    status = Column(String, default="waiting_for_opponent") # waiting_for_opponent, in_progress, finished, cancelled
    creator_move = Column(String, nullable=True) # rock, paper, scissors
    opponent_move = Column(String, nullable=True)
    creator_score = Column(Integer, default=0)
    opponent_score = Column(Integer, default=0)
    winner_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = relationship("User", foreign_keys=[creator_id], back_populates="created_game_rooms")
    opponent = relationship("User", foreign_keys=[opponent_id], back_populates="joined_game_rooms")
    winner = relationship("User", foreign_keys=[winner_id]) # No back_populates for winner to avoid recursive relationship for winner

# Database connection for Railway (PostgreSQL) or local (SQLite)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///xewee_data.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL); Base.metadata.create_all(engine); Session = sessionmaker(bind=engine); db_session = Session()

# --- Conversation States (Explicitly Defined) ---
# THERE ARE EXACTLY 23 STATES (0-22)
TASK_DESC, TASK_LINK, TASK_REWARD, REJECT_REASON_WD, BROADCAST_MESSAGE, ANNOUNCEMENT_TEXT, \
NEW_CODE_CODE, NEW_CODE_REWARD, NEW_CODE_USES, USER_MGT_ID, USER_MGT_ACTION, USER_MGT_DURATION, \
RAIN_AMOUNT, RAIN_USERS, SUBMIT_TASK_REJECT_REASON, DELETE_TASK, DELETE_CODE, WARN_USER_ID, \
WARN_REASON, USER_SEARCH_INPUT, ADJUST_BALANCE_ID, ADJUST_BALANCE_AMOUNT, ADJUST_BALANCE_CONFIRM = range(23)

# --- Bot & API Lifespan ---
ptb_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lifespan startup...")
    await ptb_app.initialize()
    await ptb_app.updater.start_polling(drop_pending_updates=True)
    await ptb_app.start()
    logger.info("Telegram bot has started successfully.")
    yield
    logger.info("Lifespan shutdown..."); await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- FastAPI Endpoints ---
@app.get("/")
async def health_check():
    return {"status": "ok", "message": f"{BOT_USERNAME} API is running!"}

@app.post("/get_initial_data")
async def get_initial_data(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        if not user_id: 
            logger.warning("get_initial_data: user_id not provided in request.")
            raise HTTPException(status_code=400, detail="user_id not provided")
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db:
            tg_user_info = None
            try:
                tg_chat = await ptb_app.bot.get_chat(user_id)
                tg_user_info = tg_chat
            except Exception as e:
                logger.warning(f"Could not fetch Telegram user info for {user_id} during initial data: {e}")

            user_db = User(
                id=user_id, 
                first_name=tg_user_info.first_name if tg_user_info else 'Unknown',
                last_name=tg_user_info.last_name if tg_user_info else None,
                username=tg_user_info.username if tg_user_info else None
            )
            db_session.add(user_db); db_session.commit()
            logger.info(f"New user {user_id} created via get_initial_data.")
        
        # Always try to update user's Telegram info on login for freshness
        tg_user_info = None
        try:
            tg_chat = await ptb_app.bot.get_chat(user_id)
            tg_user_info = tg_chat
            if user_db.first_name != tg_user_info.first_name or \
               user_db.last_name != tg_user_info.last_name or \
               user_db.username != tg_user_info.username:
                user_db.first_name = tg_user_info.first_name
                user_db.last_name = tg_user_info.last_name
                user_db.username = tg_user_info.username
                db_session.commit()
                logger.info(f"User {user_id} Telegram info updated on get_initial_data.")
        except Exception as e:
            logger.warning(f"Could not update Telegram user info for {user_id}: {e}")

        # Check user status for Mini App access
        if user_db.status == 'banned': 
            logger.info(f"User {user_id} attempted to access Mini App but is banned.")
            raise HTTPException(status_code=403, detail="You are permanently banned.")
        if user_db.status == 'restricted' and user_db.status_until and user_db.status_until > date.today():
            logger.info(f"User {user_id} attempted to access Mini App but is restricted until {user_db.status_until}.")
            raise HTTPException(status_code=403, detail=f"You are restricted until {user_db.status_until.strftime('%b %d')}.")
        elif user_db.status == 'restricted' and user_db.status_until and user_db.status_until <= date.today():
             user_db.status = 'active'; user_db.status_until = None; db_session.commit()
             logger.info(f"Restriction lifted for user {user_id}.")

        can_claim_daily = (user_db.last_login_date is None or user_db.last_login_date < date.today()) and user_db.daily_claim_invites >= DAILY_BONUS_INVITE_REQ
        
        tasks = db_session.query(Task).all()
        completed_task_ids_list = json.loads(user_db.completed_task_ids) if user_db.completed_task_ids else []
        available_tasks = [t for t in tasks if t.id not in completed_task_ids_list]

        withdrawals = db_session.query(Withdrawal).filter(Withdrawal.user_id == user_id).order_by(Withdrawal.created_at.desc()).all()
        announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
        withdrawal_maintenance = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()

        active_game_room = db_session.query(GameRoom).filter(
            or_(GameRoom.creator_id == user_id, GameRoom.opponent_id == user_id),
            GameRoom.status.in_(['waiting_for_opponent', 'in_progress'])
        ).first()
        
        return {
            "balance": user_db.balance,
            "gift_tickets": user_db.gift_tickets,
            "referral_count": user_db.referral_count,
            "successful_referrals": user_db.successful_referrals,
            "tasks_completed": user_db.tasks_completed,
            "daily_claim_invites": user_db.daily_claim_invites,
            "can_claim_daily": can_claim_daily,
            "daily_bonus_req": DAILY_BONUS_INVITE_REQ,
            "daily_bonus_amount": DAILY_BONUS,
            "announcement": announcement.value if announcement else "Welcome! No new announcements.",
            "tasks": [{"id": t.id, "description": t.description, "link": t.link, "reward": t.reward} for t in available_tasks],
            "withdrawals": [{"id": w.id, "amount": w.amount, "fee": w.fee, "status": w.status, "date": w.created_at.strftime('%Y-%m-%d'), "method": w.method} for w in withdrawals],
            "claimed_milestones": json.loads(user_db.claimed_milestones) if user_db.claimed_milestones else {},
            "min_withdrawal": MIN_WITHDRAWAL,
            "max_withdrawal": MAX_WITHDRAWAL,
            "withdrawal_fee_percent": WITHDRAWAL_FEE_PERCENT,
            "withdrawal_maintenance": withdrawal_maintenance.value == "true" if withdrawal_maintenance else False,
            "gift_ticket_price": GIFT_TICKET_PRICE,
            "gift_min_amount": GIFT_MIN_AMOUNT,
            "gift_max_amount": GIFT_MAX_AMOUNT,
            "gift_fee_percent": GIFT_FEE_PERCENT,
            "active_game_room_id": active_game_room.id if active_game_room else None
        }
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"API Error in get_initial_data for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/get_notifications")
async def get_notifications(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        if not user_id: 
            raise HTTPException(status_code=400, detail="user_id not provided")
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db:
            raise HTTPException(status_code=404, detail="User not found.")

        notifications = []

        task_submissions = db_session.query(TaskSubmission).filter(
            TaskSubmission.user_id == user_id,
            TaskSubmission.status != 'pending'
        ).order_by(TaskSubmission.created_at.desc()).all()

        for sub in task_submissions:
            task = db_session.query(Task).filter(Task.id == sub.task_id).first()
            if task:
                notifications.append({
                    "type": "task_submission",
                    "id": sub.id,
                    "description": task.description,
                    "reward": task.reward,
                    "status": sub.status,
                    "reason": sub.rejection_reason,
                    "date": sub.created_at.strftime('%Y-%m-%d'),
                    "is_read": sub.is_read
                })

        withdrawals = db_session.query(Withdrawal).filter(
            Withdrawal.user_id == user_id,
            Withdrawal.status != 'pending'
        ).order_by(Withdrawal.created_at.desc()).all()

        for wd in withdrawals:
            notifications.append({
                "type": "withdrawal",
                "id": wd.id,
                "amount": wd.amount,
                "method": wd.method,
                "status": wd.status,
                "reason": wd.rejection_reason,
                "date": wd.created_at.strftime('%Y-%m-%d'),
                "is_read": wd.is_read
            })
        
        user_events = db_session.query(UserEvent).filter(UserEvent.user_id == user_id).order_by(UserEvent.created_at.desc()).all()
        for event in user_events:
            notifications.append({
                "type": event.event_type,
                "id": event.id,
                "message": event.message,
                "related_id": event.related_id,
                "amount": event.amount,
                "status": "info", # Generic status for events, frontend maps this to info-bg
                "date": event.created_at.strftime('%Y-%m-%d'),
                "is_read": event.is_read
            })

        notifications.sort(key=lambda x: x['date'], reverse=True)

        return notifications

    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"API Error in get_notifications for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/mark_notifications_as_read")
async def mark_notifications_as_read(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id not provided")
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db:
            raise HTTPException(status_code=404, detail="User not found.")

        db_session.query(TaskSubmission).filter(TaskSubmission.user_id == user_id, TaskSubmission.is_read == False).update({"is_read": True})
        db_session.query(Withdrawal).filter(Withdrawal.user_id == user_id, Withdrawal.is_read == False).update({"is_read": True})
        db_session.query(UserEvent).filter(UserEvent.user_id == user_id, UserEvent.is_read == False).update({"is_read": True})
        
        db_session.commit()
        logger.info(f"User {user_id} marked all notifications as read.")
        return {"status": "success"}
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"API Error in mark_notifications_as_read for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/submit_task_proof")
async def submit_task_proof(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); task_id = data.get('task_id'); text = data.get('text'); photo_base64 = data.get('photo')
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")
        
        completed_task_ids_list = json.loads(user_db.completed_task_ids) if user_db.completed_task_ids else []
        if task_id in completed_task_ids_list:
            raise HTTPException(status_code=400, detail="You have already completed this task.")

        existing_pending_submission = db_session.query(TaskSubmission).filter(
            TaskSubmission.user_id == user_id, 
            TaskSubmission.task_id == task_id, 
            TaskSubmission.status == 'pending'
        ).first()
        if existing_pending_submission:
            raise HTTPException(status_code=400, detail="You already have a pending submission for this task.")

        submission = TaskSubmission(user_id=user_id, task_id=task_id, text_proof=text, photo_proof_base64=photo_base64, created_at=date.today())
        db_session.add(submission); db_session.commit()
        await ptb_app.bot.send_message(user_id, "✅ Your proof has been submitted for admin review!")
        
        task = db_session.query(Task).filter(Task.id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found for submission {submission.id}")
            task_description = "Unknown Task"
            task_reward = 0.0
        else:
            task_description = task.description
            task_reward = task.reward
        
        user_tg_obj = None
        try:
            user_tg_obj = await ptb_app.bot.get_chat(user_id)
        except Exception as e:
            logger.warning(f"Could not fetch live Telegram user object for {user_id}: {e}")

        username_str = f" (@{user_tg_obj.username})" if user_tg_obj and user_tg_obj.username else ""
        user_info_for_admin = f"{user_db.first_name or (user_tg_obj.first_name if user_tg_obj else 'Unknown')} (`{user_db.id}`){username_str}"

        caption = f"**New Task Submission for Review**\n\n- User: {user_info_for_admin}\n- Task: {task_description}\n- Reward: ₱{task_reward:.2f}\n- Note: {text}"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
        
        photo_data = base64.b64decode(photo_base64.split(',',1)[1])
        for admin_id in ADMIN_CHAT_IDS:
            try:
                await ptb_app.bot.send_photo(chat_id=admin_id, photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            except Exception as admin_e:
                logger.error(f"Failed to send task submission notification to admin {admin_id}: {admin_e}")

        logger.info(f"User {user_id} submitted proof for task {task_id}.")
        return {"status": "success"}
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"Error in submit_task_proof for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/redeem_code")
async def redeem_code(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); code_str = data.get('code').upper()
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")

        code = db_session.query(RedeemCode).filter(RedeemCode.code == code_str).first()
        
        if not code or (code.uses_left != -1 and code.uses_left <= 0):
            raise HTTPException(status_code=400, detail="Invalid or expired code.")

        user_db.balance += code.reward
        if code.uses_left != -1: 
            code.uses_left -= 1
        db_session.commit()
        logger.info(f"User {user_id} redeemed code {code_str} for {code.reward:.2f}.")
        return {"status": "success", "amount_rewarded": code.reward}
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"Error in redeem_code for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/claim_daily_bonus")
async def claim_daily_bonus(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")

        if user_db.last_login_date is None or user_db.last_login_date < date.today():
            if user_db.daily_claim_invites >= DAILY_BONUS_INVITE_REQ:
                user_db.balance += DAILY_BONUS
                user_db.last_login_date = date.today()
                user_db.daily_claim_invites = 0
                db_session.commit()
                await ptb_app.bot.send_message(user_id, f"🎉 Daily bonus of ₱{DAILY_BONUS:.2f} claimed! Come back tomorrow!")
                logger.info(f"User {user_id} claimed daily bonus.")
                return {"status": "success"}
            else:
                raise HTTPException(status_code=400, detail=f"Invite {DAILY_BONUS_INVITE_REQ - user_db.daily_claim_invites} more users to claim your daily bonus.")
        else:
            raise HTTPException(status_code=400, detail="Daily bonus already claimed or not yet available.")
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"API Error in claim_daily_bonus for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/submit_withdrawal")
async def submit_withdrawal(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); amount = float(data.get('amount')); method = data.get('method'); details = data.get('details')
        
        withdrawal_maintenance = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
        if withdrawal_maintenance and withdrawal_maintenance.value == "true":
            raise HTTPException(status_code=403, detail="Withdrawals are currently under maintenance. Please try again later.")

        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")
        
        if not (MIN_WITHDRAWAL <= amount <= MAX_WITHDRAWAL):
            raise HTTPException(status_code=400, detail=f"Amount must be between ₱{MIN_WITHDRAWAL:.2f} and ₱{MAX_WITHDRAWAL:.2f}.")
        
        if not method:
            raise HTTPException(status_code=400, detail="Please select a withdrawal method (GCash or Maya).")

        fee = amount * WITHDRAWAL_FEE_PERCENT
        total_deduction = amount + fee

        if user_db.balance < total_deduction:
            raise HTTPException(status_code=400, detail="Insufficient balance to cover withdrawal amount and fee.")
            
        new_withdrawal = Withdrawal(user_id=user_db.id, amount=amount, fee=fee, method=method, details=details, created_at=date.today())
        db_session.add(new_withdrawal); user_db.balance -= total_deduction; db_session.commit()
        await ptb_app.bot.send_message(user_id, f"✅ Your withdrawal request for ₱{amount:.2f} (Fee: ₱{fee:.2f}) has been submitted! Our team will review it shortly.")
        
        user_tg_obj = None
        try:
            user_tg_obj = await ptb_app.bot.get_chat(user_id)
        except Exception as e:
            logger.warning(f"Could not fetch live Telegram user object for {user_id}: {e}")

        username_str = f" (@{user_tg_obj.username})" if user_tg_obj and user_tg_obj.username else ""
        user_info_for_admin = f"{user_db.first_name or (user_tg_obj.first_name if user_tg_obj else 'Unknown')} (`{user_db.id}`){username_str}"

        admin_message = f"**New Withdrawal Request**\n\n- User: {user_info_for_admin}\n- Amount: `₱{amount:.2f}`\n- Fee: `₱{fee:.2f}`\n- Method: `{method}`\n- Details: `{details}`\n\n**Action: /admin**"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_wd_{new_withdrawal.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_wd_start_{new_withdrawal.id}")]]
        for admin_id in ADMIN_CHAT_IDS:
            try:
                await ptb_app.bot.send_message(chat_id=admin_id, text=admin_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            except Exception as admin_e:
                logger.error(f"Failed to send withdrawal notification to admin {admin_id}: {admin_e}")
        logger.info(f"User {user_id} submitted withdrawal request for {amount:.2f} via {method}.")
        return {"status": "success"}
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"Error in submit_withdrawal for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/buy_ticket")
async def buy_ticket(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")
        if user_db.balance < GIFT_TICKET_PRICE: 
            raise HTTPException(status_code=400, detail=f"Insufficient balance. You need ₱{GIFT_TICKET_PRICE:.2f} to buy a Gift Ticket.")
        user_db.balance -= GIFT_TICKET_PRICE; user_db.gift_tickets += 1; db_session.commit()
        await ptb_app.bot.send_message(user_id, f"🎉 You have successfully bought 1 Gift Ticket! You now have {user_db.gift_tickets} tickets.")
        logger.info(f"User {user_id} bought 1 gift ticket.")
        return {"status": "success"}
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"API Error in buy_ticket for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/gift_money")
async def gift_money(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); recipient_id = int(data.get('recipient_id')); amount = float(data.get('amount'))
        
        sender = db_session.query(User).filter(User.id == user_id).first()
        if not sender or sender.status != 'active': 
            raise HTTPException(status_code=403, detail="Account not active or found.")
        if sender.gift_tickets < 1: 
            raise HTTPException(status_code=400, detail="You do not have any Gift Tickets.")
        
        if user_id == recipient_id:
            raise HTTPException(status_code=400, detail="You cannot send a gift to yourself.")

        if not (GIFT_MIN_AMOUNT <= amount <= GIFT_MAX_AMOUNT): 
            raise HTTPException(status_code=400, detail=f"Amount must be between ₱{GIFT_MIN_AMOUNT:.2f} and ₱{GIFT_MAX_AMOUNT:.2f}.")
        
        fee = amount * GIFT_FEE_PERCENT
        total_deduction = amount + fee
        if sender.balance < total_deduction: 
            raise HTTPException(status_code=400, detail="Insufficient balance to cover gift amount and fee.")

        recipient = db_session.query(User).filter(User.id == recipient_id).first()
        if not recipient: 
            raise HTTPException(status_code=404, detail="Recipient user not found.")
        if recipient.status != 'active': 
            raise HTTPException(status_code=400, detail="Recipient account is not active.")

        sender.balance -= total_deduction; sender.gift_tickets -= 1
        recipient.balance += amount
        db_session.commit()

        gift_event_message = f"You received a gift of ₱{amount:.2f} from user {sender.first_name or sender.id}!"
        user_event = UserEvent(user_id=recipient_id, event_type='gift_received', message=gift_event_message, related_id=user_id, amount=amount)
        db_session.add(user_event); db_session.commit()

        await ptb_app.bot.send_message(user_id, f"✅ You have successfully gifted ₱{amount:.2f} to user {recipient_id}. A fee of ₱{fee:.2f} was applied.")
        await ptb_app.bot.send_message(recipient_id, f"🎉 You have received a gift of ₱{amount:.2f} from user {sender.first_name or sender.id}!")
        logger.info(f"User {user_id} gifted {amount:.2f} to user {recipient_id}.")
        return {"status": "success"}
    except HTTPException as he: 
        db_session.rollback()
        raise he
    except Exception as e: 
        db_session.rollback()
        logger.error(f"API Error in gift_money for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Game Endpoints ---
@app.post("/games/create_room")
async def create_game_room(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); bet_amount = float(data.get('bet_amount')); room_name = data.get('room_name')
        
        creator = db_session.query(User).filter(User.id == user_id).first()
        if not creator or creator.status != 'active':
            raise HTTPException(status_code=403, detail="Account not active or found.")
        if bet_amount <= 0:
            raise HTTPException(status_code=400, detail="Bet amount must be positive.")
        if creator.balance < bet_amount:
            raise HTTPException(status_code=400, detail="Insufficient balance to create room.")
        
        existing_game = db_session.query(GameRoom).filter(
            or_(GameRoom.creator_id == user_id, GameRoom.opponent_id == user_id),
            GameRoom.status.in_(['waiting_for_opponent', 'in_progress'])
        ).first()
        if existing_game:
            raise HTTPException(status_code=400, detail=f"You are already in an active game room (ID: {existing_game.id}). Please leave it first.")

        creator.balance -= bet_amount # Deduct bet from creator's balance

        new_room = GameRoom(
            room_name=room_name,
            creator_id=user_id,
            bet_amount=bet_amount,
            status='waiting_for_opponent'
        )
        db_session.add(new_room); db_session.commit()

        logger.info(f"User {user_id} created game room {new_room.id} with bet {bet_amount}.")
        return {"status": "success", "room_id": new_room.id, "room_name": new_room.room_name or f"Room {new_room.id}", "bet_amount": new_room.bet_amount}
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error creating game room for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/games/list_rooms")
async def list_game_rooms(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        
        rooms = db_session.query(GameRoom).filter(
            GameRoom.status == 'waiting_for_opponent',
            GameRoom.creator_id != user_id
        ).all()

        room_list = []
        for room in rooms:
            creator_user = db_session.query(User).filter(User.id == room.creator_id).first()
            creator_name = creator_user.first_name if creator_user else "Unknown"
            room_list.append({
                "room_id": room.id,
                "room_name": room.room_name or f"Room {room.id}",
                "creator_id": room.creator_id,
                "creator_name": creator_name,
                "bet_amount": room.bet_amount
            })
        
        return room_list
    except Exception as e:
        logger.error(f"Error listing game rooms for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/games/join_room")
async def join_game_room(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); room_id = data.get('room_id')
        
        joiner = db_session.query(User).filter(User.id == user_id).first()
        room = db_session.query(GameRoom).filter(GameRoom.id == room_id).first()

        if not joiner or joiner.status != 'active':
            raise HTTPException(status_code=403, detail="Account not active or found.")
        if not room:
            raise HTTPException(status_code=404, detail="Game room not found or no longer exists.")
        if room.creator_id == user_id:
            raise HTTPException(status_code=400, detail="You cannot join your own room.")
        if room.status != 'waiting_for_opponent':
            raise HTTPException(status_code=400, detail="Room is not waiting for an opponent. It might have started or been cancelled.")
        if joiner.balance < room.bet_amount:
            raise HTTPException(status_code=400, detail="Insufficient balance to join this room.")

        existing_game = db_session.query(GameRoom).filter(
            or_(GameRoom.creator_id == user_id, GameRoom.opponent_id == user_id),
            GameRoom.status.in_(['waiting_for_opponent', 'in_progress'])
        ).first()
        if existing_game:
            raise HTTPException(status_code=400, detail=f"You are already in an active game room (ID: {existing_game.id}). Please leave it first.")

        joiner.balance -= room.bet_amount # Deduct bet from joiner's balance

        room.opponent_id = user_id
        room.status = 'in_progress'
        room.updated_at = datetime.utcnow()
        db_session.commit()

        creator_name = room.creator.first_name or "Your opponent"
        joiner_name = joiner.first_name or "Your opponent"

        await ptb_app.bot.send_message(room.creator_id, f"🎉 An opponent ({joiner_name}) has joined your room '{room.room_name or room.id}'! Game starts now!")
        
        logger.info(f"User {user_id} joined game room {room_id}. Game in progress.")
        return {"status": "success", "room_id": room.id, "room_name": room.room_name or f"Room {room.id}"}
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error joining game room {room_id} for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/games/make_move")
async def make_game_move(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); room_id = data.get('room_id'); move = data.get('move')
        
        if move not in ['rock', 'paper', 'scissors']:
            raise HTTPException(status_code=400, detail="Invalid move. Choose 'rock', 'paper', or 'scissors'.")

        room = db_session.query(GameRoom).filter(GameRoom.id == room_id).first()
        if not room:
            raise HTTPException(status_code=404, detail="Game room not found.")
        if room.status != 'in_progress':
            raise HTTPException(status_code=400, detail="Game is not in progress or has already finished/cancelled.")
        if user_id not in [room.creator_id, room.opponent_id]:
            raise HTTPException(status_code=403, detail="You are not a participant in this game.")

        is_creator = (user_id == room.creator_id)

        if is_creator:
            if room.creator_move:
                raise HTTPException(status_code=400, detail="You have already made your move for this round. Waiting for opponent.")
            room.creator_move = move
        else:
            if room.opponent_move:
                raise HTTPException(status_code=400, detail="You have already made your move for this round. Waiting for opponent.")
            room.opponent_move = move
        
        room.updated_at = datetime.utcnow()
        db_session.commit()

        round_result_message = ""
        current_game_status = "in_progress"
        final_result_message = None

        if room.creator_move and room.opponent_move:
            creator_name = room.creator.first_name or "Creator"
            opponent_name = room.opponent.first_name or "Opponent"

            if room.creator_move == room.opponent_move:
                round_result_message = "It's a draw!"
            elif (room.creator_move == 'rock' and room.opponent_move == 'scissors') or \
                 (room.creator_move == 'paper' and room.opponent_move == 'rock') or \
                 (room.creator_move == 'scissors' and room.opponent_move == 'paper'):
                room.creator_score += 1
                round_result_message = f"{creator_name} wins the round!"
            else:
                room.opponent_score += 1
                round_result_message = f"{opponent_name} wins the round!"
            
            winner = None
            if room.creator_score >= GAME_TARGET_SCORE:
                winner = room.creator
                final_result_message = f"Game Over! {winner.first_name or 'Creator'} wins the game!"
                current_game_status = 'finished'
            elif room.opponent_score >= GAME_TARGET_SCORE:
                winner = room.opponent
                final_result_message = f"Game Over! {winner.first_name or 'Opponent'} wins the game!"
                current_game_status = 'finished'
            
            if winner:
                room.status = current_game_status
                room.winner_id = winner.id
                
                winnings = room.bet_amount * 2
                winner.balance += winnings
                db_session.commit()

                await ptb_app.bot.send_message(room.creator_id, f"🏆 {final_result_message} You won ₱{winnings:.2f}!")
                await ptb_app.bot.send_message(room.opponent_id, f"😔 {final_result_message} You lost ₱{room.bet_amount:.2f}.")
                logger.info(f"Game {room_id} finished. Winner: {winner.id}. Payout: {winnings}.")
                
                winner_event_msg = f"You won ₱{winnings:.2f} in a game against {(room.opponent.first_name if winner.id == room.creator_id else room.creator.first_name) or 'an opponent'}!"
                winner_event = UserEvent(user_id=winner.id, event_type='game_won', message=winner_event_msg, related_id=room.id, amount=winnings)
                
                loser_id = room.opponent_id if winner.id == room.creator_id else room.creator_id
                loser_name = (room.opponent.first_name if loser_id == room.opponent_id else room.creator.first_name) or 'an opponent'
                loser_event_msg = f"You lost ₱{room.bet_amount:.2f} in a game against {winner.first_name or 'an opponent'}!"
                loser_event = UserEvent(user_id=loser_id, event_type='game_lost', message=loser_event_msg, related_id=room.id, amount=-room.bet_amount)
                
                db_session.add_all([winner_event, loser_event]); db_session.commit()
            else: # Game continues, reset moves for next round
                room.creator_move = None
                room.opponent_move = None
                db_session.commit()
                await ptb_app.bot.send_message(room.creator_id, f"Round Result: {round_result_message} Current Score: {room.creator_score}-{room.opponent_score}. Make your next move!")
                await ptb_app.bot.send_message(room.opponent_id, f"Round Result: {round_result_message} Current Score: {room.opponent_score}-{room.creator_score}. Make your next move!")

        logger.info(f"User {user_id} made move '{move}' in room {room_id}. Game Status: {room.status}")
        return {
            "status": "success", 
            "game_id": room.id, 
            "room_name": room.room_name,
            "creator_score": room.creator_score,
            "opponent_score": room.opponent_score,
            "last_round_result": round_result_message,
            "game_status": current_game_status,
            "final_result_message": final_result_message,
            "player_score": room.creator_score if is_creator else room.opponent_score,
            "opponent_score_display": room.opponent_score if is_creator else room.creator_score,
            "your_last_move": room.creator_move if is_creator else room.opponent_move,
            "opponent_last_move": room.opponent_move if is_creator else room.creator_move,
            "your_move_made": bool(room.creator_move if is_creator else room.opponent_move),
            "opponent_move_made": bool(room.opponent_move if is_creator else room.creator_move),
            "opponent_name": room.opponent.first_name if room.opponent else "Opponent"
        }
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error making move for user {user_id} in room {room_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/games/get_game_state")
async def get_game_state(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); room_id = data.get('room_id')
        
        room = db_session.query(GameRoom).filter(GameRoom.id == room_id).first()
        if not room:
            raise HTTPException(status_code=404, detail="Game room not found or already removed.")
        if user_id not in [room.creator_id, room.opponent_id]:
            raise HTTPException(status_code=403, detail="You are not a participant in this game.")
        
        is_creator = (user_id == room.creator_id)

        if room.status == 'waiting_for_opponent' and (datetime.utcnow() - room.created_at).total_seconds() > GAME_ROOM_INACTIVITY_TIMEOUT_MIN * 60:
            room.status = 'cancelled'
            room.creator.balance += room.bet_amount
            db_session.commit()
            await ptb_app.bot.send_message(room.creator_id, f"🚫 Your game room '{room.room_name or room.id}' was cancelled due to inactivity.")
            logger.info(f"Game room {room_id} cancelled due to inactivity.")
            raise HTTPException(status_code=404, detail="Game ended due to inactivity.")
        
        if room.status == 'finished':
            winner_name = room.winner.first_name if room.winner else "Draw"
            final_message = f"Game Over! {winner_name} wins! Final Scores: {room.creator_score}-{room.opponent_score}."
            return {
                "room_id": room.id,
                "room_name": room.room_name,
                "status": "finished",
                "creator_score": room.creator_score,
                "opponent_score": room.opponent_score,
                "player_score": room.creator_score if is_creator else room.opponent_score,
                "opponent_score_display": room.opponent_score if is_creator else room.creator_score,
                "winner_name": winner_name,
                "final_result_message": final_message,
                "your_last_move": room.creator_move if is_creator else room.opponent_move,
                "opponent_last_move": room.opponent_move if is_creator else room.creator_move,
                "opponent_name": room.opponent.first_name if room.opponent else "Opponent"
            }

        return {
            "room_id": room.id,
            "room_name": room.room_name,
            "status": room.status,
            "creator_id": room.creator_id,
            "opponent_id": room.opponent_id,
            "bet_amount": room.bet_amount,
            "creator_score": room.creator_score,
            "opponent_score": room.opponent_score,
            "player_score": room.creator_score if is_creator else room.opponent_score,
            "opponent_score_display": room.opponent_score if is_creator else room.creator_score,
            "your_move_made": bool(room.creator_move if is_creator else room.opponent_move),
            "opponent_move_made": bool(room.opponent_move if is_creator else room.creator_move),
            "your_last_move": room.creator_move if is_creator else room.opponent_move,
            "opponent_last_move": room.opponent_move if is_creator else room.creator_move,
            "opponent_name": room.opponent.first_name if room.opponent else "Opponent"
        }
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error getting game state for user {user_id} in room {room_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/games/leave_room")
async def leave_game_room(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); room_id = data.get('room_id')
        
        room = db_session.query(GameRoom).filter(GameRoom.id == room_id).first()
        if not room:
            raise HTTPException(status_code=404, detail="Game room not found or already removed.")
        if user_id not in [room.creator_id, room.opponent_id]:
            raise HTTPException(status_code=403, detail="You are not a participant in this game.")
        if room.status == 'finished' or room.status == 'cancelled':
            raise HTTPException(status_code=400, detail="Game is already finished or cancelled.")

        leaving_player_id = user_id
        opponent_player_id = room.opponent_id if room.creator_id == user_id else room.creator_id
        
        leaving_player_db = db_session.query(User).filter(User.id == leaving_player_id).first()
        opponent_player_db = db_session.query(User).filter(User.id == opponent_player_id).first()

        if leaving_player_db:
            leaving_player_db.balance += room.bet_amount
            await ptb_app.bot.send_message(leaving_player_id, f"You left the game '{room.room_name or room.id}'. Your bet of ₱{room.bet_amount:.2f} has been refunded.")

        room.status = 'cancelled'
        room.updated_at = datetime.utcnow()
        db_session.commit()

        if opponent_player_id and opponent_player_db:
            opponent_player_db.balance += room.bet_amount
            db_session.commit()
            await ptb_app.bot.send_message(opponent_player_id, f"🚫 Your opponent left the game '{room.room_name or room.id}'. The game has been cancelled and your bet of ₱{room.bet_amount:.2f} has been refunded.")
        
        logger.info(f"User {user_id} left game room {room_id}. Game cancelled.")
        return {"status": "success", "message": "Game left and cancelled."}
    except HTTPException as he:
        db_session.rollback()
        raise he
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error leaving game room {room_id} for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# --- Telegram Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_db = db_session.query(User).filter(User.id == user.id).first()
    
    if not user_db:
        user_db = User(id=user.id, first_name=user.first_name, last_name=user.last_name, username=user.username)
        db_session.add(user_db)
        logger.info(f"New user {user.id} ({user.first_name}) registered via start command.")
    elif user_db.first_name != user.first_name or user_db.last_name != user.last_name or user_db.username != user.username:
        user_db.first_name = user.first_name
        user_db.last_name = user.last_name
        user_db.username = user.username
        db_session.commit()
        logger.info(f"User {user.id} info updated.")

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id and (user_db.referrer_id is None) and db_session.query(User).filter(User.id == referrer_id).first():
                user_db.referrer_id = referrer_id
                referrer = db_session.query(User).filter(User.id == referrer_id).first()
                if referrer:
                    referrer.referral_count += 1
                    referrer.daily_claim_invites += 1
                    
                    referrer_event_message = f"🎉 {user.first_name or 'A friend'} has joined using your link!"
                    referrer_event = UserEvent(user_id=referrer.id, event_type='referral_join', message=referrer_event_message, related_id=user.id)
                    db_session.add(referrer_event)

                    await context.bot.send_message(chat_id=referrer.id, text=f"🎉 {user.first_name or 'A friend'} has joined using your link! Encourage them to complete tasks to earn commissions!")
                    logger.info(f"User {user.id} (first_name: {user.first_name}) referred by {referrer_id}. Referrer's daily invites incremented.")
                else:
                    logger.warning(f"Referrer {referrer_id} not found for new user {user.id}.")
            elif referrer_id == user.id:
                logger.info(f"User {user.id} tried to refer themselves.")
            elif user_db.referrer_id is not None:
                logger.info(f"User {user.id} already has a referrer ({user_db.referrer_id}), ignoring new referral link.")
        except (ValueError, IndexError) as e: 
            logger.warning(f"Invalid referrer_id in start command for user {user.id}: {context.args[0]} - {e}")
    
    db_session.commit()

    if user_db.status == 'banned': 
        caption = "🚫 You are permanently banned from this bot."; keyboard = []
    elif user_db.status == 'restricted' and user_db.status_until and user_db.status_until > date.today():
        caption = f"⚠️ Your account is restricted until {user_db.status_until.strftime('%b %d')}."; keyboard = []
    else:
        caption = (f"🚀 **Greetings, {user.first_name}!**\n\nWelcome to **{BOT_USERNAME}**, your portal to earning real rewards. Embark on quests (tasks), recruit allies (referrals), and claim your treasure.\n\nYour adventure begins now. Launch the dashboard to get started!")
        keyboard = [[InlineKeyboardButton("📱 Launch Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))]]
    
    try:
        await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send start message to user {user.id}: {e}")

# --- Admin Panel Handlers ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: 
        logger.warning(f"Unauthorized admin access attempt by {update.effective_user.id}.")
        return 
    
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📜 Set Announcement", callback_data="admin_set_announcement"), InlineKeyboardButton("📝 Manage Tasks", callback_data="admin_manage_tasks")],
        [InlineKeyboardButton("🔑 Manage Codes", callback_data="admin_manage_codes"), InlineKeyboardButton("🔨 User Management", callback_data="admin_user_mgt")],
        [InlineKeyboardButton("🔍 User Search", callback_data="admin_user_search"), InlineKeyboardButton("💰 Adjust Balance", callback_data="admin_adjust_balance")], 
        [InlineKeyboardButton("🌧️ Rain Prize", callback_data="admin_rain"), InlineKeyboardButton("🧐 Review Submissions", callback_data="admin_pending_submissions")],
        [InlineKeyboardButton("⚙️ Withdrawal Maintenance", callback_data="admin_maintenance")],
        [InlineKeyboardButton("⚠️ Warn User", callback_data="admin_warn_user")]
    ]
    await update.message.reply_text("👑 **Xewee Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📜 Set Announcement", callback_data="admin_set_announcement"), InlineKeyboardButton("📝 Manage Tasks", callback_data="admin_manage_tasks")],
        [InlineKeyboardButton("🔑 Manage Codes", callback_data="admin_manage_codes"), InlineKeyboardButton("🔨 User Management", callback_data="admin_user_mgt")],
        [InlineKeyboardButton("🔍 User Search", callback_data="admin_user_search"), InlineKeyboardButton("💰 Adjust Balance", callback_data="admin_adjust_balance")],
        [InlineKeyboardButton("🌧️ Rain Prize", callback_data="admin_rain"), InlineKeyboardButton("🧐 Review Submissions", callback_data="admin_pending_submissions")],
        [InlineKeyboardButton("⚙️ Withdrawal Maintenance", callback_data="admin_maintenance")],
        [InlineKeyboardButton("⚠️ Warn User", callback_data="admin_warn_user")]
    ]
    try:
        await query.message.edit_text("👑 **Xewee Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception:
        await query.message.reply_text("👑 **Xewee Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    total_users = db_session.query(User).count()
    active_users = db_session.query(User).filter(User.status == 'active').count()
    banned_users = db_session.query(User).filter(User.status == 'banned').count()
    restricted_users = db_session.query(User).filter(User.status == 'restricted').count()
    total_balance = sum(u.balance for u in db_session.query(User).all())
    pending_withdrawals = db_session.query(Withdrawal).filter(Withdrawal.status == 'pending').count()
    pending_submissions = db_session.query(TaskSubmission).filter(TaskSubmission.status == 'pending').count()
    active_game_rooms = db_session.query(GameRoom).filter(GameRoom.status == 'in_progress').count()
    waiting_game_rooms = db_session.query(GameRoom).filter(GameRoom.status == 'waiting_for_opponent').count()


    stats_message = (
        f"**📊 Bot Statistics:**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"⛔ Banned Users: {banned_users}\n"
        f"🚧 Restricted Users: {restricted_users}\n"
        f"💰 Total Balance in Circulation: ₱{total_balance:.2f}\n"
        f"💸 Pending Withdrawals: {pending_withdrawals}\n"
        f"📝 Pending Task Submissions: {pending_submissions}\n"
        f"🎮 Active Game Rooms: {active_game_rooms}\n"
        f"🕹️ Waiting Game Rooms: {waiting_game_rooms}"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
    await query.message.edit_text(stats_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Broadcast Conversation ---
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Send the message you want to broadcast to all active users. (Markdown supported)\n\nTo cancel, send /cancel."); 
    return BROADCAST_MESSAGE
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return

    active_users = db_session.query(User).filter(User.status == 'active').all()
    sent_count = 0
    failed_count = 0
    for user_db in active_users: 
        try: 
            await context.bot.copy_message(chat_id=user_db.id, from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
            sent_count += 1
        except Exception as e: 
            logger.error(f"Failed to broadcast to {user_db.id}: {e}")
            failed_count += 1
    
    await update.message.reply_text(f"Broadcast sent to {sent_count} active users. Failed for {failed_count} users."); 
    return ConversationHandler.END

# --- Announcement Conversation ---
async def announcement_start(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    current_announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
    current_text = current_announcement.value if current_announcement else "No current announcement."
    await query.message.reply_text(f"Current Announcement:\n`{current_text}`\n\nEnter new announcement text (or send /clear to remove). To cancel, send /cancel."); 
    return ANNOUNCEMENT_TEXT

async def set_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return

    announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
    if not announcement: 
        announcement = SystemInfo(key='announcement')
    
    try:
        if update.message.text.lower() == '/clear': 
            if announcement.value:
                db_session.delete(announcement)
                await update.message.reply_text("Announcement cleared.")
                logger.info(f"Admin {update.effective_user.id} cleared announcement.")
            else:
                await update.message.reply_text("No announcement to clear.")
        else: 
            announcement.value = update.message.text
            db_session.add(announcement)
            await update.message.reply_text("Announcement set.")
            logger.info(f"Admin {update.effective_user.id} set announcement: {update.message.text[:50]}...")
        
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error setting/clearing announcement: {e}")
        await update.message.reply_text("An error occurred while setting/clearing the announcement.")

    return ConversationHandler.END

# --- Manage Tasks ---
async def manage_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer(); 
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    keyboard = [[InlineKeyboardButton("➕ Add New Task", callback_data="add_task_start")], 
                [InlineKeyboardButton("🗑️ Remove Task", callback_data="remove_task_list")],
                [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
    await query.message.edit_text("Manage tasks:", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Enter task description (max 255 chars):\n\nTo cancel, send /cancel."); 
    return TASK_DESC

async def get_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    if len(update.message.text) > 255:
        await update.message.reply_text("Description too long. Max 255 characters. Please try again:");
        return TASK_DESC
    context.user_data['task_desc'] = update.message.text
    await update.message.reply_text("Send the link (e.g., https://example.com):\n\nTo cancel, send /cancel."); 
    return TASK_LINK

async def get_task_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    link = update.message.text
    if not link.startswith('http://') and not link.startswith('https://'): 
        link = 'https://' + link
    context.user_data['task_link'] = link
    await update.message.reply_text("Enter the reward amount (e.g., 50.00):\n\nTo cancel, send /cancel."); 
    return TASK_REWARD

async def get_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        reward = float(update.message.text)
        if reward <= 0:
            await update.message.reply_text("Reward must be a positive number. Please try again:");
            return TASK_REWARD
        
        # Correctly add the task after all validations
        db_session.add(Task(description=context.user_data['task_desc'], link=context.user_data['task_link'], reward=reward))
        db_session.commit() 
        await update.message.reply_text("✅ Task added!"); 
        logger.info(f"Admin {update.effective_user.id} added task: {context.user_data['task_desc']}.")
        return ConversationHandler.END
    except ValueError: 
        await update.message.reply_text("Invalid amount. Please enter a number (e.g., 50.00):"); 
        return TASK_REWARD
    except Exception as e: # Catch any unexpected errors during task addition
        db_session.rollback()
        logger.error(f"Error adding task: {e}")
        await update.message.reply_text("An error occurred while adding the task.")
        return ConversationHandler.END


async def remove_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); 
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    tasks = db_session.query(Task).all()
    if not tasks: 
        await query.message.edit_text("No tasks to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Task Management", callback_data="admin_manage_tasks")]]))
        return
    
    keyboard = [[InlineKeyboardButton(f"❌ {task.description[:40]} (₱{task.reward:.2f})", callback_data=f"delete_task_{task.id}")] for task in tasks]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Task Management", callback_data="admin_manage_tasks")])
    await query.message.edit_text("Select a task to remove:", reply_markup=InlineKeyboardMarkup(keyboard));

async def delete_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    task_id = int(query.data.split("_")[2]); 
    task = db_session.query(Task).filter(Task.id == task_id).first()
    if task: 
        try:
            db_session.delete(task); 
            db_session.commit(); 
            await query.answer("Task removed!", show_alert=True); 
            logger.info(f"Admin {query.from_user.id} deleted task {task_id}.")
            await remove_task_list(update, context)
        except Exception as e:
            db_session.rollback()
            logger.error(f"Error deleting task {task_id}: {e}")
            await query.answer("An error occurred while deleting the task.", show_alert=True)
            await remove_task_list(update, context)
    else: 
        await query.answer("Task not found.", show_alert=True)
        await remove_task_list(update, context)

# --- Manage Codes ---
async def manage_codes(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer(); 
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    keyboard = [[InlineKeyboardButton("➕ Add New Code", callback_data="add_code_start")], 
                [InlineKeyboardButton("🗑️ Remove Code", callback_data="remove_code_list")],
                [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
    await query.message.edit_text("Manage redeem codes:", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Enter new redeem code (e.g., XEWEEBONUS):\n\nTo cancel, send /cancel."); 
    return NEW_CODE_CODE

async def get_new_code_code(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    code_text = update.message.text.upper()
    if db_session.query(RedeemCode).filter(RedeemCode.code == code_text).first():
        await update.message.reply_text(f"Code '{code_text}' already exists. Please enter a unique code:\n\nTo cancel, send /cancel.")
        return NEW_CODE_CODE
    context.user_data['new_code_code'] = code_text
    await update.message.reply_text("Enter reward amount (e.g., 100.00):\n\nTo cancel, send /cancel."); 
    return NEW_CODE_REWARD

async def get_new_code_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        reward = float(update.message.text)
        if reward <= 0:
            await update.message.reply_text("Reward must be a positive number. Please try again:");
            return NEW_CODE_REWARD
        context.user_data['new_code_reward'] = reward
        await update.message.reply_text("Enter uses left (-1 for unlimited, 1 for single-use):\n\nTo cancel, send /cancel."); 
        return NEW_CODE_USES 
    except ValueError: 
        await update.message.reply_text("Invalid amount. Please enter a number (e.g., 100.00):"); 
        return NEW_CODE_REWARD

async def get_new_code_uses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try:
        uses = int(update.message.text)
        if uses < -1 or uses == 0:
            await update.message.reply_text("Uses left must be -1 (unlimited) or a positive integer. Please try again:");
            return NEW_CODE_USES

        code = RedeemCode(code=context.user_data['new_code_code'], reward=context.user_data['new_code_reward'], uses_left=uses)
        db_session.add(code); 
        db_session.commit(); 
        await update.message.reply_text("✅ Code added!"); 
        logger.info(f"Admin {update.effective_user.id} added redeem code {code.code}.")
        return ConversationHandler.END
    except ValueError: 
        await update.message.reply_text("Invalid uses. Please enter an integer (e.g., 5, or -1):"); 
        return NEW_CODE_USES
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error adding redeem code: {e}")
        await update.message.reply_text("An error occurred while adding the redeem code.")
        return ConversationHandler.END

async def remove_code_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); 
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    codes = db_session.query(RedeemCode).all()
    if not codes: 
        await query.message.edit_text("No codes to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Code Management", callback_data="admin_manage_codes")]]))
        return
    
    keyboard = [[InlineKeyboardButton(f"❌ {code.code} (₱{code.reward:.2f}, uses: {code.uses_left})", callback_data=f"delete_code_{code.id}")] for code in codes]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Code Management", callback_data="admin_manage_codes")])
    await query.message.edit_text("Select a code to remove:", reply_markup=InlineKeyboardMarkup(keyboard));

async def delete_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    
    code_id = int(query.data.split("_")[2]); 
    code = db_session.query(RedeemCode).filter(RedeemCode.id == code_id).first()
    if code: 
        try:
            db_session.delete(code); 
            db_session.commit(); 
            await query.answer("Code removed!", show_alert=True); 
            logger.info(f"Admin {query.from_user.id} deleted redeem code {code.code}.")
            await remove_code_list(update, context)
        except Exception as e:
            db_session.rollback()
            logger.error(f"Error deleting redeem code {code.id}: {e}")
            await query.answer("An error occurred while deleting the code.", show_alert=True)
            await remove_code_list(update, context)
    else: 
        await query.answer("Code not found.", show_alert=True)
        await remove_code_list(update, context)

# --- User Management ---
async def user_mgt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    keyboard = [[InlineKeyboardButton("Ban 🔨", callback_data="user_mgt_ban")], 
                [InlineKeyboardButton("Unban 🔓", callback_data="user_mgt_unban")], 
                [InlineKeyboardButton("Restrict Temp ⏳", callback_data="user_mgt_restrict")],
                [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
    await query.message.edit_text("User Management:", reply_markup=InlineKeyboardMarkup(keyboard))

async def user_mgt_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    context.user_data['mgt_action'] = query.data.split('_')[-1]
    await query.message.reply_text(f"Send the User ID to {context.user_data['mgt_action']}: (Send /cancel to abort)"); 
    return USER_MGT_ID

async def user_mgt_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    
    try: 
        user_id = int(update.message.text)
        if user_id in ADMIN_CHAT_IDS and context.user_data['mgt_action'] != 'unban':
            await update.message.reply_text("You cannot ban/restrict an admin. Please enter a different User ID.");
            return USER_MGT_ID
        context.user_data['mgt_user_id'] = user_id
    except ValueError: 
        await update.message.reply_text("Invalid User ID. Please enter a numerical ID:"); 
        return ConversationHandler.END
    
    if context.user_data['mgt_action'] == 'restrict': 
        await update.message.reply_text("Enter duration in days (e.g., 7):\n\nTo cancel, send /cancel."); 
        return USER_MGT_DURATION
    
    user_db = db_session.query(User).filter(User.id == user_id).first() 
    if not user_db: 
        await update.message.reply_text("User not found."); 
        return ConversationHandler.END
    
    try:
        if context.user_data['mgt_action'] == 'ban': 
            user_db.status = 'banned'; user_db.status_until = None; 
            await ptb_app.bot.send_message(user_id, "⚠️ Your account has been permanently banned from Xewee.")
            await update.message.reply_text("User permanently banned.");
            logger.info(f"Admin {update.effective_user.id} banned user {user_id}.")
        elif context.user_data['mgt_action'] == 'unban': 
            user_db.status = 'active'; user_db.status_until = None; 
            await ptb_app.bot.send_message(user_id, "✅ Your Xewee account has been unbanned.")
            await update.message.reply_text("User unbanned.");
            logger.info(f"Admin {update.effective_user.id} unbanned user {user_id}.")
        
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error during user management action {context.user_data['mgt_action']} for user {user_id}: {e}")
        await update.message.reply_text("An error occurred. Please try again.")

    return ConversationHandler.END

async def user_mgt_duration_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        duration = int(update.message.text)
        if duration <= 0:
            await update.message.reply_text("Duration must be a positive number of days. Please try again:");
            return USER_MGT_DURATION
        user_id = context.user_data['mgt_user_id']
    except ValueError: 
        await update.message.reply_text("Invalid duration. Please enter a number of days:"); 
        return ConversationHandler.END
    
    user_db = db_session.query(User).filter(User.id == user_id).first() 
    if not user_db: 
        await update.message.reply_text("User not found."); 
        return ConversationHandler.END
    
    try:
        user_db.status = 'restricted'; user_db.status_until = date.today() + timedelta(days=duration)
        db_session.commit()
        await ptb_app.bot.send_message(user_id, f"⚠️ Your Xewee account has been temporarily restricted for {duration} days.") 
        await update.message.reply_text(f"User {user_id} restricted for {duration} days.");
        logger.info(f"Admin {update.effective_user.id} restricted user {user_id} for {duration} days.")
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error restricting user {user_id}: {e}")
        await update.message.reply_text("An error occurred. Please try again.")

    return ConversationHandler.END

# --- User Search ---
async def user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Send the User ID or Telegram Username to search (e.g., 123456789 or @username).\n\nTo cancel, send /cancel.");
    return USER_SEARCH_INPUT

async def user_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    search_query = update.message.text.strip()
    
    user_db = None
    user_tg_obj = None

    try:
        user_id = int(search_query)
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if user_db:
            try:
                user_tg_obj = await ptb_app.bot.get_chat(user_db.id)
            except Exception as e:
                logger.warning(f"Could not fetch Telegram user object for ID {user_db.id}: {e}")
    except ValueError:
        username_query = search_query
        if username_query.startswith('@'):
            username_query = username_query[1:]
        
        user_db = db_session.query(User).filter(User.username == username_query).first()
        if user_db:
            try:
                user_tg_obj = await ptb_app.bot.get_chat(user_db.id)
            except Exception as e:
                logger.warning(f"Could not fetch Telegram user object for DB username match {user_db.id}: {e}")
        else:
            try:
                user_chat_obj = await ptb_app.bot.get_chat(f"@{username_query}")
                if user_chat_obj.type == 'private' and user_chat_obj.id:
                    user_db = db_session.query(User).filter(User.id == user_chat_obj.id).first()
                    if user_db:
                        user_tg_obj = user_chat_obj
                    else:
                        user_db = User(id=user_chat_obj.id, first_name=user_chat_obj.first_name, last_name=user_chat_obj.last_name, username=user_chat_obj.username)
                        db_session.add(user_db); db_session.commit()
                        user_tg_obj = user_chat_obj
            except Exception as e:
                logger.warning(f"Telegram API search for @{username_query} failed or not a private chat: {e}")
    
    if user_db:
        username_display = f"@{user_db.username}" if user_db.username else (f"@{user_tg_obj.username}" if user_tg_obj and user_tg_obj.username else "N/A")
        first_name_display = user_db.first_name or (user_tg_obj.first_name if user_tg_obj else "Unknown")
        referrer_info = f"Referred by: `{user_db.referrer_id}`" if user_db.referrer_id else "No referrer"

        user_info_msg = (
            f"**User Found!**\n\n"
            f"- Name: {first_name_display}\n"
            f"- ID: `{user_db.id}`\n"
            f"- Username: {username_display}\n"
            f"- Status: `{user_db.status.upper()}`{' (until ' + user_db.status_until.strftime('%b %d') + ')' if user_db.status_until and user_db.status == 'restricted' else ''}\n"
            f"- Balance: ₱{user_db.balance:.2f}\n"
            f"- Gift Tickets: {user_db.gift_tickets}\n"
            f"- Tasks Completed: {user_db.tasks_completed}\n"
            f"- Total Referrals: {user_db.referral_count}\n"
            f"- Successful Referrals: {user_db.successful_referrals}\n"
            f"- {referrer_info}\n"
        )
        await update.message.reply_text(user_info_msg, parse_mode='Markdown')
        logger.info(f"Admin {update.effective_user.id} searched for user '{search_query}', found {user_db.id}.")
    else:
        await update.message.reply_text(f"User '{search_query}' not found in database or Telegram API.")
        logger.info(f"Admin {update.effective_user.id} searched for user '{search_query}', not found.")
    
    return ConversationHandler.END


# --- Adjust Balance ---
async def adjust_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Send the User ID to adjust balance for:\n\nTo cancel, send /cancel.");
    return ADJUST_BALANCE_ID

async def get_adjust_balance_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try:
        user_id = int(update.message.text)
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db:
            await update.message.reply_text("User not found. Please enter a valid User ID:");
            return ADJUST_BALANCE_ID
        context.user_data['adjust_user_id'] = user_id
        await update.message.reply_text(f"Current balance for {user_db.first_name} (`{user_db.id}`): ₱{user_db.balance:.2f}\n\nEnter amount to adjust (e.g., 100.00 to add, -50.00 to subtract):\n\nTo cancel, send /cancel.");
        return ADJUST_BALANCE_AMOUNT
    except ValueError:
        await update.message.reply_text("Invalid User ID. Please enter a numerical ID:");
        return ConversationHandler.END

async def get_adjust_balance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try:
        amount = float(update.message.text)
        context.user_data['adjust_amount'] = amount
        user_id = context.user_data['adjust_user_id']
        user_db = db_session.query(User).filter(User.id == user_id).first()
        
        action = "add" if amount >= 0 else "subtract"
        confirmation_msg = (
            f"Confirm balance adjustment for {user_db.first_name} (`{user_db.id}`):\n"
            f"Action: {action.capitalize()} ₱{abs(amount):.2f}\n"
            f"Current Balance: ₱{user_db.balance:.2f}\n"
            f"New Balance: ₱{(user_db.balance + amount):.2f}\n\n"
            f"Reply 'yes' to confirm or /cancel to abort."
        )
        await update.message.reply_text(confirmation_msg, parse_mode='Markdown')
        return ADJUST_BALANCE_CONFIRM
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number (e.g., 100.00 or -50.00):");
        return ADJUST_BALANCE_AMOUNT

async def confirm_adjust_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    
    if update.message.text.lower() == 'yes':
        user_id = context.user_data.get('adjust_user_id')
        amount = context.user_data.get('adjust_amount')
        user_db = db_session.query(User).filter(User.id == user_id).first()
        
        if user_db:
            try:
                user_db.balance += amount
                db_session.commit()
                await update.message.reply_text(f"✅ Balance adjusted for {user_db.first_name} (`{user_db.id}`). New balance: ₱{user_db.balance:.2f}")
                await ptb_app.bot.send_message(user_id, f"💰 Your Xewee balance has been adjusted by an admin by ₱{amount:.2f}. New balance: ₱{user_db.balance:.2f}")
                logger.info(f"Admin {update.effective_user.id} adjusted balance for user {user_id} by {amount:.2f}.")
            except Exception as e:
                db_session.rollback()
                logger.error(f"Error adjusting balance for user {user_id}: {e}")
                await update.message.reply_text("An error occurred while adjusting balance.")
        else:
            await update.message.reply_text("User not found during confirmation. Balance not adjusted.")
    else:
        await update.message.reply_text("Balance adjustment cancelled.")
    
    return ConversationHandler.END


# --- Rain Prize ---
async def rain_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Send the total amount to distribute (e.g., 500):\n\nTo cancel, send /cancel."); 
    return RAIN_AMOUNT

async def rain_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Amount must be positive. Please try again:");
            return RAIN_AMOUNT
        context.user_data['rain_amount'] = amount
    except ValueError: 
        await update.message.reply_text("Invalid amount. Please enter a number:"); 
        return ConversationHandler.END
    await update.message.reply_text("Send the number of users to share the prize pool with (e.g., 10):\n\nTo cancel, send /cancel."); 
    return RAIN_USERS

async def rain_users_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        num_users = int(update.message.text)
        if num_users <= 0:
            await update.message.reply_text("Number of users must be positive. Please try again:");
            return RAIN_USERS
        amount = context.user_data['rain_amount']
    except ValueError: 
        await update.message.reply_text("Invalid number. Please enter an integer:"); 
        return ConversationHandler.END
    
    eligible_users = db_session.query(User).filter(User.status == 'active').all()
    if len(eligible_users) < num_users: 
        await update.message.reply_text(f"Only {len(eligible_users)} eligible active users found. Cannot rain to {num_users} users."); 
        return ConversationHandler.END
    
    winners = random.sample(eligible_users, num_users)
    prize_per_user = amount / num_users
    
    try:
        for user_db in winners: 
            user_db.balance += prize_per_user
            await ptb_app.bot.send_message(user_db.id, f"🎉 You were in the Xewee Rain Prize! You won ₱{prize_per_user:.2f}!")
            user_event = UserEvent(user_id=user_db.id, event_type='rain_prize_won', message=f"You won ₱{prize_per_user:.2f} in a Rain Prize!", related_id=None, amount=prize_per_user)
            db_session.add(user_event)
        db_session.commit()
        await update.message.reply_text(f"Rain Prize complete. ₱{amount:.2f} distributed to {num_users} users."); 
        logger.info(f"Admin {update.effective_user.id} distributed rain prize of {amount:.2f} to {num_users} users.")
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error distributing rain prize: {e}")
        await update.message.reply_text("An error occurred during rain prize distribution. Funds were rolled back.")
    return ConversationHandler.END

# --- Review Submissions ---
async def review_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query; await query.answer()
        if query.from_user.id not in ADMIN_CHAT_IDS: return
        message_to_edit = query.message
    else:
        message_to_edit = context.user_data.get('admin_submission_message')

    submission = db_session.query(TaskSubmission).filter(TaskSubmission.status == 'pending').first()
    if not submission: 
        if message_to_edit:
            await message_to_edit.edit_text("No pending submissions to review.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]))
        else:
            await context.bot.send_message(update.effective_chat.id, "No pending submissions to review.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]))
        if 'admin_submission_message' in context.user_data:
            del context.user_data['admin_submission_message']
        return
    
    user_db = db_session.query(User).filter(User.id == submission.user_id).first() 
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    
    if not user_db or not task:
        logger.error(f"Review Submission: User ({submission.user_id}) or task ({submission.task_id}) not found for submission ID {submission.id}. Marking as rejected.")
        submission.status = 'rejected'; submission.rejection_reason = "User or task not found."
        db_session.commit()
        if message_to_edit:
            await message_to_edit.edit_text(f"Skipping submission {submission.id}: Associated user or task not found. Checking next...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next Submission", callback_data="admin_pending_submissions")]]))
        else:
            await context.bot.send_message(update.effective_chat.id, f"Skipping submission {submission.id}: Associated user or task not found. Checking next...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next Submission", callback_data="admin_pending_submissions")]]))
        await review_submissions(update, context)
        return

    user_tg_obj = None
    try:
        user_tg_obj = await ptb_app.bot.get_chat(user_db.id)
    except Exception as e:
        logger.warning(f"Could not fetch live Telegram user object for {user_db.id}: {e}")

    username_display = f"@{user_tg_obj.username}" if user_tg_obj and user_tg_obj.username else "N/A"
    first_name_display = user_db.first_name or (user_tg_obj.first_name if user_tg_obj else "Unknown")
    
    caption_text = f"**Submission Review (ID: {submission.id})**\n\n- User: {first_name_display} (`{user_db.id}`){f' {username_display}' if username_display != 'N/A' else ''}\n- Task: {task.description}\n- Reward: ₱{task.reward:.2f}\n- Note: {submission.text_proof}\n\nSubmitted on: {submission.created_at.strftime('%Y-%m-%d')}"
    keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
    
    context.user_data['admin_submission_message'] = message_to_edit
    
    try:
        photo_data = base64.b64decode(submission.photo_proof_base64.split(',',1)[1])
        
        if message_to_edit and message_to_edit.photo:
            if message_to_edit.caption != caption_text:
                await message_to_edit.edit_media(
                    media=InputFile(BytesIO(photo_data), filename=f"submission_{submission.id}.png"), 
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    caption=caption_text,
                    parse_mode='Markdown'
                )
            else:
                logger.info(f"Submission {submission.id} photo message caption not modified, skipping edit media.")
        elif message_to_edit and message_to_edit.text:
            await message_to_edit.delete()
            message_to_edit = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=InputFile(BytesIO(photo_data), filename=f"submission_{submission.id}.png"), caption=caption_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            context.user_data['admin_submission_message'] = message_to_edit
        else:
            message_to_edit = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=InputFile(BytesIO(photo_data), filename=f"submission_{submission.id}.png"), caption=caption_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            context.user_data['admin_submission_message'] = message_to_edit
        
    except Exception as e:
        logger.error(f"Failed to display photo for submission {submission.id}: {e}. Sending as text instead.")
        new_text_content = caption_text + "\n\n*(Failed to display image proof)*"
        if message_to_edit:
            if message_to_edit.text != new_text_content or message_to_edit.reply_markup != InlineKeyboardMarkup(keyboard):
                await message_to_edit.edit_text(new_text_content, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            else:
                logger.info(f"Submission {submission.id} text message content not modified, skipping edit.")
        else:
            message_to_edit = await context.bot.send_message(update.effective_chat.id, new_text_content, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            context.user_data['admin_submission_message'] = message_to_edit


async def approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    sub_id = int(query.data.split("_")[2]); 
    submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    
    if not submission or submission.status != 'pending': 
        current_caption = query.message.caption if query.message.caption else ""
        new_caption = f"{current_caption}\n\n**Status: Already processed.**"
        if current_caption != new_caption:
            await query.edit_message_caption(caption=new_caption, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for submission {sub_id} already shows processed. Skipping edit.")
        return
    
    user_db = db_session.query(User).filter(User.id == submission.user_id).first() 
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    
    if not user_db or user_db.status != 'active':
        submission.status = 'rejected' 
        submission.rejection_reason = "User account not active."
        db_session.commit()
        current_caption = query.message.caption if query.message.caption else ""
        new_caption = f"{current_caption}\n\n**Status: REJECTED (User not active or found)**"
        if current_caption != new_caption:
            await query.edit_message_caption(caption=new_caption, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for submission {sub_id} already shows rejected. Skipping edit.")
        logger.warning(f"Submission {sub_id} rejected: User {submission.user_id} not active or found.")
        await review_submissions(update, context)
        return
    if not task:
        submission.status = 'rejected' 
        submission.rejection_reason = "Associated task not found."
        db_session.commit()
        current_caption = query.message.caption if query.message.caption else ""
        new_caption = f"{current_caption}\n\n**Status: REJECTED (Task not found)**"
        if current_caption != new_caption:
            await query.edit_message_caption(caption=new_caption, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for submission {sub_id} already shows rejected. Skipping edit.")
        logger.warning(f"Submission {sub_id} rejected: Task {submission.task_id} not found.")
        await review_submissions(update, context)
        return

    submission.status = 'approved'
    
    try:
        completed_task_ids_list = json.loads(user_db.completed_task_ids) if user_db.completed_task_ids else []
        if task.id not in completed_task_ids_list:
            user_db.balance += task.reward
            user_db.tasks_completed += 1
            completed_task_ids_list.append(task.id)
            user_db.completed_task_ids = json.dumps(completed_task_ids_list)

            if user_db.referrer_id: 
                referrer = db_session.query(User).filter(User.id == user_db.referrer_id).first()
                if referrer and referrer.status == 'active': 
                    commission_amount = task.reward * REFERRAL_COMMISSION_PERCENT
                    referrer.balance += commission_amount
                    if user_db.tasks_completed == 1:
                        referrer.successful_referrals += 1
                    
                    referrer_event_message = f"Your referred friend {user_db.first_name or 'A friend'} completed a task ('{task.description}') and you earned ₱{commission_amount:.2f}!"
                    referrer_event = UserEvent(user_id=referrer.id, event_type='referral_commission', message=referrer_event_message, related_id=user_db.id, amount=commission_amount)
                    db_session.add(referrer_event)

                    await ptb_app.bot.send_message(
                        referrer.id, 
                        f"🎉 Your referred friend {user_db.first_name or 'A friend'} completed a task ('{task.description}') and you earned ₱{commission_amount:.2f} ({(REFERRAL_COMMISSION_PERCENT*100):.0f}% commission)!"
                    )
                    logger.info(f"Referral commission of {commission_amount} given to {referrer.id} for {user_db.id}'s task {task.id}.")
                elif referrer:
                    logger.info(f"Referrer {referrer.id} for user {user_db.id} is not active, no commission awarded.")
                else:
                    logger.warning(f"Referrer {user_db.referrer_id} not found for user {user_db.id}.")
            
            claimed_milestones = json.loads(user_db.claimed_milestones) if user_db.claimed_milestones else {}
            for milestone_str, reward_amount in TASK_MILESTONES.items():
                milestone = int(milestone_str.split('_')[0]) 
                if user_db.tasks_completed >= milestone and claimed_milestones.get(milestone_str) is None:
                    user_db.balance += reward_amount
                    claimed_milestones[milestone_str] = True
                    user_db.claimed_milestones = json.dumps(claimed_milestones) 
                    await ptb_app.bot.send_message(user_db.id, f"🎉 Milestone Reached! You completed {milestone} tasks and earned a bonus of ₱{reward_amount:.2f}!")
                    logger.info(f"User {user_db.id} reached milestone {milestone} tasks and claimed {reward_amount:.2f}.")
            
            db_session.commit()
            await ptb_app.bot.send_message(chat_id=user_db.id, text=f"🎉 Your submission for '{task.description}' was approved! You earned ₱{task.reward:.2f}.")
            logger.info(f"Submission {sub_id} approved for user {user_db.id}, task {task.id}. User balance updated.")
        else:
            db_session.commit()
            await ptb_app.bot.send_message(chat_id=user_db.id, text=f"✅ Your submission for '{task.description}' was approved. (Reward already processed for this task).")
            logger.info(f"Submission {sub_id} approved for user {user_db.id}, task {task.id}. Task already completed, no new reward.")

        current_caption = query.message.caption if query.message.caption else ""
        new_caption = f"{current_caption}\n\n**Status: APPROVED**"
        if current_caption != new_caption:
            await query.edit_message_caption(caption=new_caption, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for submission {sub_id} already shows APPROVED. Skipping edit.")

        await review_submissions(update, context)

    except Exception as e:
        db_session.rollback()
        logger.error(f"Error approving submission {sub_id} for user {user_db.id}: {e}", exc_info=True)
        await query.message.reply_text(f"An error occurred while approving submission {sub_id}. Funds might be rolled back.")
        await review_submissions(update, context)


async def reject_submission_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    sub_id = int(query.data.split("_")[3])
    submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    
    if not submission or submission.status != 'pending': 
        current_caption = query.message.caption if query.message.caption else ""
        new_caption = f"{current_caption}\n\n**Status: Already processed.**"
        if current_caption != new_caption:
            await query.edit_message_caption(caption=new_caption, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for submission {sub_id} already shows processed. Skipping edit.")
        return

    context.user_data['submission_id_to_reject'] = sub_id
    context.user_data['original_submission_caption'] = query.message.caption if query.message.caption else ""
    await query.message.reply_text("Please provide a brief reason for rejecting this submission (or send /skip for no reason).\n\nTo cancel, send /cancel."); 
    return SUBMIT_TASK_REJECT_REASON

async def get_submission_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return

    sub_id = context.user_data.get('submission_id_to_reject')
    original_caption = context.user_data.get('original_submission_caption', "Submission Review")
    submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    
    if not submission or submission.status != 'pending': 
        await update.message.reply_text("Submission already processed or not found."); 
        return ConversationHandler.END
    
    reason = "No reason provided." if update.message.text.lower() == '/skip' else update.message.text
    
    try:
        submission.status = 'rejected'; 
        submission.rejection_reason = reason
        db_session.commit()
        await update.message.reply_text(f"❌ Submission #{sub_id} has been rejected.")
        task = db_session.query(Task).filter(Task.id == submission.task_id).first()
        task_description = task.description if task else "Unknown Task"
        await ptb_app.bot.send_message(chat_id=submission.user_id, text=f"⚠️ Your submission for '{task_description}' was rejected.\n\n**Admin's Remark:** {reason}", parse_mode='Markdown')
        logger.info(f"Admin {update.effective_user.id} rejected submission {sub_id} for user {submission.user_id}.")

        admin_message_to_edit = context.user_data.get('admin_submission_message')
        if admin_message_to_edit:
            new_caption = f"{original_caption}\n\n**Status: REJECTED**\nReason: {reason}"
            if admin_message_to_edit.caption != new_caption:
                await admin_message_to_edit.edit_caption(caption=new_caption, parse_mode='Markdown')
            else:
                logger.info(f"Admin message for submission {sub_id} already shows REJECTED. Skipping edit.")

    except Exception as e:
        db_session.rollback()
        logger.error(f"Error rejecting submission {sub_id}: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while rejecting the submission.")

    await review_submissions(update, context)
    return ConversationHandler.END

# --- Withdrawal Handlers ---
async def approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    wd_id = int(query.data.split("_")[2]); 
    withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == wd_id).first()
    
    if not withdrawal or withdrawal.status != "pending": 
        message_content = query.message.text if query.message.text else query.message.caption if query.message.caption else ""
        new_content = f"{message_content}\n\n**Status: Already processed.**"
        if message_content != new_content:
            await query.message.edit_text(new_content, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for withdrawal {wd_id} already shows processed. Skipping edit.")
        return
    
    try:
        withdrawal.status = "approved"; 
        db_session.commit(); 
        
        message_content = query.message.text if query.message.text else query.message.caption if query.message.caption else ""
        new_content = f"{message_content}\n\n✅ **Request #{wd_id} approved.**"
        if message_content != new_content:
            await query.message.edit_text(new_content, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for withdrawal {wd_id} already shows approved. Skipping edit.")
        
        await ptb_app.bot.send_message(chat_id=withdrawal.user_id, text=f"🎉 Good news! Your withdrawal of ₱{withdrawal.amount:.2f} has been approved and sent.")
        logger.info(f"Admin {query.from_user.id} approved withdrawal {wd_id} for user {withdrawal.user_id}.")
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error approving withdrawal {wd_id}: {e}", exc_info=True)
        await query.message.reply_text(f"An error occurred while approving withdrawal {wd_id}.")


async def reject_withdrawal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    wd_id = int(query.data.split("_")[3])
    withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == wd_id).first()
    
    if not withdrawal or withdrawal.status != 'pending': 
        message_content = query.message.text if query.message.text else query.message.caption if query.message.caption else ""
        new_content = f"{message_content}\n\n**Status: Already processed.**"
        if message_content != new_content:
            await query.message.edit_text(new_content, parse_mode='Markdown')
        else:
            logger.info(f"Admin message for withdrawal {wd_id} already shows processed. Skipping edit.")
        return

    context.user_data['withdrawal_id_to_reject'] = wd_id
    context.user_data['original_withdrawal_message_content'] = query.message.text if query.message.text else query.message.caption if query.message.caption else ""
    context.user_data['admin_withdrawal_message_object'] = query.message
    await query.message.reply_text("Please provide a brief reason for rejecting this withdrawal (or send /skip).\n\nTo cancel, send /cancel."); 
    return REJECT_REASON_WD

async def get_withdrawal_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return

    wd_id = context.user_data.get('withdrawal_id_to_reject')
    original_content = context.user_data.get('original_withdrawal_message_content', "Withdrawal Request")
    withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == wd_id).first()
    
    if not withdrawal or withdrawal.status != 'pending': 
        await update.message.reply_text("Withdrawal already processed or not found."); 
        return ConversationHandler.END
    
    reason = "No reason provided." if update.message.text.lower() == '/skip' else update.message.text
    user_db = db_session.query(User).filter(User.id == withdrawal.user_id).first(); 
    
    try:
        if withdrawal.status == 'pending' and user_db:
            user_db.balance += withdrawal.amount + withdrawal.fee
            await ptb_app.bot.send_message(chat_id=user_db.id, text=f"⚠️ Your withdrawal request of ₱{withdrawal.amount:.2f} was rejected and the amount returned to your balance.\n\n**Admin's Remark:** {reason}", parse_mode='Markdown')
        elif not user_db:
            logger.warning(f"User {withdrawal.user_id} not found for withdrawal {wd_id}, cannot return funds.")
        
        withdrawal.status = "rejected"; 
        withdrawal.rejection_reason = reason
        db_session.commit()
        await update.message.reply_text(f"❌ Request #{wd_id} has been rejected.")
        logger.info(f"Admin {update.effective_user.id} rejected withdrawal {wd_id} for user {withdrawal.user_id}.")

        admin_message_to_edit = context.user_data.get('admin_withdrawal_message_object')
        if admin_message_to_edit:
            new_content = f"{original_content}\n\n❌ **Request #{wd_id} rejected.**\nReason: {reason}"
            if admin_message_to_edit.text != new_content:
                await admin_message_to_edit.edit_text(new_content, parse_mode='Markdown')
            else:
                logger.info(f"Admin message for withdrawal {wd_id} already shows rejected. Skipping edit.")
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error rejecting withdrawal {wd_id}: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while rejecting the withdrawal.")

    return ConversationHandler.END

# --- Maintenance Mode Toggle ---
async def maintenance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    maintenance_info = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    current_status_text = "ENABLED ✅" if maintenance_info and maintenance_info.value == "true" else "DISABLED ❌"
    toggle_button_text = f"Turn {'OFF' if current_status_text.startswith('ENABLED') else 'ON'}"
    
    keyboard = [[InlineKeyboardButton(toggle_button_text, callback_data="toggle_maintenance")],
                [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
    
    new_message_text = f"Withdrawal Maintenance is currently {current_status_text}"
    if query.message.text != new_message_text or query.message.reply_markup != InlineKeyboardMarkup(keyboard):
        await query.message.edit_text(new_message_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        logger.info("Maintenance status message not modified, skipping edit.")


async def toggle_maintenance_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.from_user.id not in ADMIN_CHAT_IDS: return

    maintenance_info = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    if not maintenance_info: 
        maintenance_info = SystemInfo(key='withdrawal_maintenance', value='false')

    new_status_value = "true" if maintenance_info.value == "false" else "false"
    maintenance_info.value = new_status_value
    
    try:
        db_session.add(maintenance_info); db_session.commit()
        status_text = "ENABLED ✅" if new_status_value == "true" else "DISABLED ❌"
        toggle_button_text = f"Turn {'OFF' if new_status_value=='true' else 'ON'}"
        keyboard = [[InlineKeyboardButton(toggle_button_text, callback_data="toggle_maintenance")],
                    [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu")]]
        
        new_message_text = f"Withdrawal Maintenance is now {status_text}"
        if query.message.text != new_message_text or query.message.reply_markup != InlineKeyboardMarkup(keyboard):
            await query.message.edit_text(new_message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            logger.info(f"Maintenance status message not modified by toggle, skipping edit.")

        logger.info(f"Admin {query.from_user.id} toggled withdrawal maintenance to {new_status_value}.")
    except Exception as e:
        db_session.rollback()
        logger.error(f"Error toggling maintenance mode: {e}")
        await query.message.reply_text("An error occurred while toggling maintenance mode.")


# --- Warn User Conversation ---
async def warn_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); 
    if query.from_user.id not in ADMIN_CHAT_IDS: return
    await query.message.reply_text("Send the User ID to warn:\n\nTo cancel, send /cancel."); 
    return WARN_USER_ID

async def get_warn_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    try: 
        user_id = int(update.message.text)
        if user_id in ADMIN_CHAT_IDS:
            await update.message.reply_text("You cannot warn an admin. Please enter a different User ID.");
            return USER_MGT_ID
        context.user_data['warn_user_id'] = user_id
    except ValueError: 
        await update.message.reply_text("Invalid User ID. Please enter a numerical ID:"); 
        return ConversationHandler.END
    await update.message.reply_text("Send the warning message/reason:\n\nTo cancel, send /cancel."); 
    return WARN_REASON

async def send_warn_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    user_id = context.user_data.get('warn_user_id')
    reason = update.message.text
    user_db = db_session.query(User).filter(User.id == user_id).first()
    if user_db:
        try:
            await ptb_app.bot.send_message(user_id, f"⚠️ **Warning from Xewee Admin:** {reason}", parse_mode='Markdown')
            await update.message.reply_text(f"Warning sent to user {user_id}.")
            logger.info(f"Admin {update.effective_user.id} sent warning to user {user_id}.")
        except Exception as e:
            logger.error(f"Failed to send warning message to user {user_id}: {e}")
            await update.message.reply_text(f"Failed to send warning to user {user_id}.")
    else:
        await update.message.reply_text(f"User {user_id} not found.")
    return ConversationHandler.END


# --- Conversation Cancellation Handler ---
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_CHAT_IDS: return
    await update.message.reply_text("Operation cancelled.")
    if context.user_data:
        context.user_data.clear()
    return ConversationHandler.END

# --- Add Handlers to the PTB Application ---
ptb_app.add_handler(CommandHandler("start", start_command))
ptb_app.add_handler(CommandHandler("admin", admin_command))

cancel_handler = CommandHandler("cancel", cancel_conversation)

ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(broadcast_start, pattern="^admin_broadcast$")], 
    states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message)]}, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(announcement_start, pattern="^admin_set_announcement$")], 
    states={ANNOUNCEMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_announcement_text)]}, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(add_task_start, pattern="^add_task_start$")], 
    states={
        TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_description)], 
        TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_link)], 
        TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_reward)]
    }, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(add_code_start, pattern="^add_code_start$")], 
    states={
        NEW_CODE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_code)], 
        NEW_CODE_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_reward)], 
        NEW_CODE_USES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_uses)]
    }, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(user_mgt_action_callback, pattern=r"^user_mgt_(ban|unban|restrict)$")], 
    states={
        USER_MGT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_mgt_id_input)], 
        USER_MGT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_mgt_duration_input)]
    }, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(rain_start, pattern="^admin_rain$")], 
    states={
        RAIN_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rain_amount_input)], 
        RAIN_USERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rain_users_input)]
    }, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(reject_withdrawal_start, pattern=r"^reject_wd_start_\d+$")], 
    states={REJECT_REASON_WD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_withdrawal_rejection_reason)]}, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(reject_submission_start, pattern=r"^reject_sub_start_\d+$")], 
    states={SUBMIT_TASK_REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_submission_rejection_reason)]}, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(warn_user_start, pattern="^admin_warn_user$")], 
    states={
        WARN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_warn_user_id)], 
        WARN_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_warn_message)]
    }, 
    fallbacks=[cancel_handler], 
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(user_search_start, pattern="^admin_user_search$")],
    states={USER_SEARCH_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_search_input)]},
    fallbacks=[cancel_handler],
    per_user=True, per_chat=False
))
ptb_app.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(adjust_balance_start, pattern="^admin_adjust_balance$")],
    states={
        ADJUST_BALANCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_adjust_balance_id)],
        ADJUST_BALANCE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_adjust_balance_amount)],
        ADJUST_BALANCE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_adjust_balance)]
    },
    fallbacks=[cancel_handler],
    per_user=True, per_chat=False
))


# Callback Query Handlers (non-conversation specific)
ptb_app.add_handler(CallbackQueryHandler(admin_main_menu_callback, pattern="^admin_main_menu$"))
ptb_app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
ptb_app.add_handler(CallbackQueryHandler(manage_tasks, pattern="^admin_manage_tasks$"))
ptb_app.add_handler(CallbackQueryHandler(remove_task_list, pattern="^remove_task_list$"))
ptb_app.add_handler(CallbackQueryHandler(delete_task_callback, pattern=r"^delete_task_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(manage_codes, pattern="^admin_manage_codes$"))
ptb_app.add_handler(CallbackQueryHandler(remove_code_list, pattern="^remove_code_list$"))
ptb_app.add_handler(CallbackQueryHandler(delete_code_callback, pattern=r"^delete_code_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(user_mgt_start, pattern="^admin_user_mgt$")) 
ptb_app.add_handler(CallbackQueryHandler(review_submissions, pattern="^admin_pending_submissions$"))
ptb_app.add_handler(CallbackQueryHandler(approve_submission, pattern=r"^approve_sub_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(approve_withdrawal, pattern=r"^approve_wd_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(maintenance_start, pattern="^admin_maintenance$"))
ptb_app.add_handler(CallbackQueryHandler(toggle_maintenance_mode, pattern="^toggle_maintenance$"))


# --- Main Entry ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)