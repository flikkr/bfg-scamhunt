from telegram import Update
from telegram.ext import ContextTypes

from bot.messages import ScamHuntMessages as messages
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.handler.utils import (
    BotStates,
    CallbackData,
    get_inline_cancel_confirm_keyboard,
)

from bot.onboarding.onboarding import is_onboarding, onboarding
from bot.handler import commands

from bot.openai.ocr import ocr_image, Platform
from bot.openai.embeddings import get_embedding
import logging
from bot.db import report, embeddings
from datetime import datetime
from bot.user_metrics import track_user_event, Event
from bot.feedback import process_feedback, is_feedback
from bot.db.user import create_user_if_not_exists


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the callback query from the inline keyboard."""
    track_user_event(update, context)
    if is_onboarding(update.callback_query.data):
        await onboarding(update, context)
        return
    if is_feedback(update.callback_query.data):
        await process_feedback(update, context)
        return
    query = update.callback_query
    await query.answer()
    match query.data:
        case CallbackData.REPORT_SCAM:
            await commands.report(update, context)
        case CallbackData.CANCEL:
            track_user_event(update, context, Event.CANCEL)
            await query.edit_message_text(
                text=messages.cancel + messages.end_message,
                parse_mode="Markdown",
            )
        case CallbackData.CONFIRM:
                await confirm_screenshot(update, context)
        case CallbackData.FEEDBACK:
            await commands.feedback(update, context)


async def confirm_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_user_event(update, context, Event.CONFIRM_SCREENSHOT)
    query = update.callback_query
    await query.edit_message_text(
        text=messages.looking_into_scam,
        parse_mode="Markdown",
    )
    image = await context.bot.get_file(context.user_data["photo"].file_id)
    result, exception = await ocr_image(image)
    embed_result, embed_exception = await get_embedding(
        f"{result.caption} {result.description}"
    )
    if exception or embed_exception:
        await query.edit_message_text(
            text=messages.error,
            parse_mode="Markdown",
        )
        return
    context.user_data["embedding"] = embed_result.embedding
    context.user_data["state"] = BotStates.START
    if not result.is_screenshot or result.platform == Platform.UNKNOWN:
        logging.error(f"Invalid platform: {result.platform}")
        logging.error(f"Is screenshot: {result.is_screenshot}")
        text = ("Oops! 🙈 It looks like this isn't a screenshot or we couldn't identify the platform.\n\n"
                "Please try again.")
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
        )
        return
    context.user_data["report"] = report.Report(
        platform=result.platform,
        from_user=result.from_user,
        to_user=result.to_user,
        caption=result.caption,
        location=result.location,
        description=result.description,
        reasoning=result.reasoning,
        scam_likelihood=result.scam_likelihood,
        is_advertisement=result.is_advertisement,
        is_sponsored=result.is_sponsored,
        is_photo=result.is_photo,
        is_video=result.is_video,
        is_social_media_post=result.is_social_media_post,
        created_by_tg_id=update.effective_user.id,  # Using the Telegram user's ID
        created_at=datetime.now().isoformat(),
        scam_types=[scam_type.dict() for scam_type in result.scam_types],
        links=result.links,
        phone_numbers=result.phone_numbers,
        emails=result.emails,
        likes=result.likes,
        comments=result.comments,
        shares=result.shares,
    )

    text = f"Seems like you shared a suspicious *{result.platform}* post. Do you want to report it?"
    await query.edit_message_text(
        text=text,
        reply_markup=get_inline_cancel_confirm_keyboard(),
        parse_mode="Markdown",
    )

    create_user_if_not_exists(update, context)
    r, err = report.create_report(context.user_data["report"])
    track_user_event(update, context, Event.REPORT_CREATED)
    if err is None and "embedding" in context.user_data and "id" in r:
        embeddings.insert_embedding(context.user_data["embedding"], r["id"])
    else:
        logging.error(f"Report created without embedding or id: {err}")
    await update.callback_query.edit_message_text(
        text=messages.confirm,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Hunt on Facebook", url="https://www.facebook.com")
                ],
                [
                    InlineKeyboardButton("Hunt on Instagram", url="https://www.instagram.com")
                ],
                [
                    InlineKeyboardButton("Report suspicious post", callback_data=CallbackData.REPORT_SCAM)
                ],
                [
                    InlineKeyboardButton("Share your feedback", callback_data=CallbackData.FEEDBACK)
                ],
            ]
        )
    )
    if context.user_data["is_new"]:
        await commands.feedback(update, context)
