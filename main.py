"""
Main entry point for Telegram Email Image Delivery Bot.
Initializes database, configures handlers, sets Telegram '/' UI command menu with command validation,
populates global in-memory BOT_SETTINGS, AUTH_USERS_CACHE, CLIENT_GROUPS_CACHE, and LOADERS_CACHE on startup,
starts background tasks, and runs bot polling.
"""

import re
import sys
import asyncio
import logging
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

from config import Config
from database import (
    init_db,
    cleanup_old_records,
    check_order_timeouts,
    reload_bot_settings_cache,
    reload_auth_users_cache,
    reload_loaders_cache,
    reload_global_client_prices_cache,
    reload_loader_prices_cache
)
from utils import setup_logging, get_all_admin_user_ids
from handlers import (
    source_group_handler,
    edited_message_handler,
    delivery_group_handler,
    duplicate_order_callback_handler,
    category_b_approval_callback_handler,
    price_callback_handler,
    price_input_text_handler,
    loader_issue_callback_handler,
    customer_confirmation_callback_handler,
    redeliver_callback_handler,
    unknown_package_price_callback_handler,
    loader_pkg_toggle_callback_handler,
    loader_pkg_confirm_callback_handler,
    loader_pkg_cancel_callback_handler,
    category_a_command,
    category_b_command,
    category_check_command,
    remove_category_command,
    paymentgroup_command,
    paymentverification_command_handler,
    walletverification_command_handler,
    approve_order_command,
    reject_order_command,
    loaderadd_command,
    loaderlist_command,
    loaderremove_command,
    loader_text_wizard_handler,
    user_command,
    users_command,
    start_command,
    help_command,
    dashboard_command,
    find_command,
    order_info_command,
    cancel_command,
    client_cancel_command_handler,
    client_cancellation_request_callback_handler,
    topup_command_handler,
    wallet_command_handler,
    testbinance_command_handler,
    resend_command,
    delete_command,
    stats_command,
    pending_command,
    delivered_command,
    export_command,
    backup_command,
    restore_command,
    setup_command,
    source_command,
    delivery_command,
    groups_command,
    status_command,
    removesource_command,
    removedelivery_command,
    resetgroups_command,
    exportprices_command_handler,
    loaderexportprice_command_handler,
    updateprices_command_handler,
    setclientprice_command_handler,
    setloaderprice_command_handler,
    bulk_price_update_text_handler,
    undo_command_handler,
    ledger_undo_callback_handler,
    addprice_command_handler,
    subtractprice_command_handler,
    ledger_command_handler,
    todaytotal_command_handler,
    resetledger_command_handler,
    calculate_command_handler,
    total_command_handler,
    calc_undo_command_handler,
    calc_undo_callback_handler,
    running_total_command_handler,
    pay_running_total_command_handler,
    manual_running_total_text_handler,
    running_total_undo_command_handler,
    running_total_undo_callback_handler,
    pendingorders_command_handler,
    order_lookup_command_handler,
    order_status_command_handler,
    assignloader_command_handler,
    reassignloader_command_handler,
    myorders_command_handler,
    revieworders_command_handler,
    completedorders_command_handler,
    cancelledorders_command_handler,
    failedorders_command_handler,
    retryorder_command_handler,
    operational_pagination_callback_handler,
    admin_profit_code_completion_handler,
    parse_admin_profit_code_input
)

# Initialize application logging
setup_logging()
logger = logging.getLogger("main")


class ProfitCodeInputFilter(filters.MessageFilter):
    """Filter to detect admin profit code submission inputs like 'test@example.com\\nV' or '#46 V'."""
    def filter(self, message) -> bool:
        if not message:
            return False
        text = message.text or message.caption or ""
        if not text:
            return False
        return parse_admin_profit_code_input(text) is not None


def validate_bot_command(cmd: BotCommand) -> bool:
    """
    Validates a Telegram BotCommand against Telegram API rules:
    - Name: lowercase letters (a-z), digits (0-9), underscore (_), length 1-32.
    - Description: length 1-256.
    """
    name_pattern = r'^[a-z0-9_]{1,32}$'
    if not re.match(name_pattern, cmd.command):
        return False
    if not (1 <= len(cmd.description) <= 256):
        return False
    return True


async def periodic_maintenance_task() -> None:
    """Background task running every hour for order timeouts and 24h database retention cleanup."""
    while True:
        try:
            await asyncio.sleep(3600)  # Check every hour
            # Check order timeouts (pending longer than 24 hours)
            expired = await check_order_timeouts(timeout_hours=24)
            if expired > 0:
                logger.info(f"Periodic check marked {expired} pending order(s) as Expired (⏰ Pending Too Long).")

            # Retention cleanup if configured
            if Config.CLEANUP_DAYS > 0:
                await cleanup_old_records(Config.CLEANUP_DAYS)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic maintenance task: {e}")


async def post_init(application: Application) -> None:
    """Post-initialization callback run inside the active application event loop."""
    logger.info("Initializing database schema...")
    await init_db()

    # Load Settings, Authorized Users, Client Groups, Loaders, and Price Caches from DB once on startup into RAM
    await reload_bot_settings_cache()
    await reload_auth_users_cache()
    await reload_loaders_cache()
    await reload_global_client_prices_cache()
    await reload_loader_prices_cache()

    # Register Clean & Frequently Used Bot Commands for Telegram '/' menu UI
    default_raw_commands = [
        BotCommand("start", "Start Bot"),
        BotCommand("help", "Help"),
        BotCommand("dashboard", "Open Live Web Dashboard & Mini-App"),
        BotCommand("setup", "Setup Guide"),
        BotCommand("source", "Set Client Group"),
        BotCommand("delivery", "Set Loader Group"),
        BotCommand("paymentgroup", "Set Payment Review Group"),
        BotCommand("paymentverification", "Toggle Payment Verification"),
        BotCommand("walletverification", "Toggle Wallet Enforcement"),
        BotCommand("a", "Set Category A"),
        BotCommand("b", "Set Category B"),
        BotCommand("category", "View Group Category"),
        BotCommand("removecategory", "Remove Category"),
        BotCommand("approve", "Approve Order"),
        BotCommand("reject", "Reject Order"),
        BotCommand("groups", "Group Configuration"),
        BotCommand("status", "Bot Status"),
        BotCommand("removesource", "Remove Client Group"),
        BotCommand("removedelivery", "Remove Loader Group"),
        BotCommand("resetgroups", "Reset Groups"),
        BotCommand("loaderadd", "Add Loader"),
        BotCommand("loaderlist", "List Loaders"),
        BotCommand("loaderremove", "Remove Loader"),
        BotCommand("user", "Manage Delivery Users"),
        BotCommand("users", "List Authorized Users"),
        BotCommand("pending", "Pending Orders"),
        BotCommand("delivered", "Delivered Orders"),
        BotCommand("find", "Find Order"),
        BotCommand("order", "Order Info"),
        BotCommand("cancel", "Cancel Order"),
        BotCommand("resend", "Resend Order"),
        BotCommand("delete", "Delete Order"),
        BotCommand("stats", "Statistics"),
        BotCommand("export", "Export Orders CSV"),
        BotCommand("backup", "Backup Database"),
        BotCommand("restore", "Restore Database"),
        BotCommand("exportprices", "Export Client Prices"),
        BotCommand("loaderexportprice", "Export Loader Prices"),
        BotCommand("updateprices", "Bulk Update Prices"),
        BotCommand("setclientprice", "Set Global Client Prices"),
        BotCommand("setloaderprice", "Set Loader Price List"),
        BotCommand("calculate", "Add or Subtract Amount"),
        BotCommand("total", "View Current Total"),
        BotCommand("pay", "Record Payment & Reset Total"),
        BotCommand("undo", "Undo Last Action"),
        BotCommand("addprice", "Add Price Adjustment"),
        BotCommand("subtractprice", "Subtract Price Adjustment"),
        BotCommand("ledger", "View Delivery Ledger"),
        BotCommand("todaytotal", "View Today Revenue & Stats"),
        BotCommand("resetledger", "Reset Running Total"),
        BotCommand("cancelorder", "Client Cancel Order Request"),
        BotCommand("topup", "Admin Top-up Customer Wallet"),
        BotCommand("wallet", "View Category B Wallet Balance"),
        BotCommand("balance", "View Category B Wallet Balance"),
        BotCommand("testbinance", "Test Binance API Connectivity"),
        BotCommand("pendingorders", "Pending Orders Operations"),
        BotCommand("assignloader", "Assign Order Loader"),
        BotCommand("reassignloader", "Reassign Order Loader"),
        BotCommand("myorders", "My Active Orders"),
        BotCommand("revieworders", "Orders Needing Review"),
        BotCommand("completedorders", "Completed Orders"),
        BotCommand("cancelledorders", "Cancelled Orders"),
        BotCommand("failedorders", "Failed Orders"),
        BotCommand("retryorder", "Retry Order")
    ]

    valid_default_commands = [cmd for cmd in default_raw_commands if validate_bot_command(cmd)]

    try:
        await application.bot.set_my_commands(valid_default_commands, scope=BotCommandScopeDefault())
        logger.info(f"[COMMANDS] Registered {len(valid_default_commands)} default bot commands.")
    except Exception:
        logger.exception("[COMMANDS] Failed to register default bot commands.")

    # Initial order timeout check on startup
    expired = await check_order_timeouts(timeout_hours=24)
    if expired > 0:
        logger.info(f"Startup check marked {expired} pending order(s) as Expired.")

    if Config.CLEANUP_DAYS > 0:
        cleaned = await cleanup_old_records(Config.CLEANUP_DAYS)
        if cleaned > 0:
            logger.info(f"Startup retention check purged {cleaned} expired records.")

    # Schedule background maintenance task in active event loop
    asyncio.create_task(periodic_maintenance_task())

    logger.info("Bot initialization complete. Active and listening for updates...")


def build_application() -> Application:
    """Builds and returns the configured Application instance with all registered handlers."""
    application = (
        ApplicationBuilder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register Setup & Group Configuration Commands (supporting both lowercase and uppercase aliases)
    application.add_handler(CommandHandler("setup", setup_command))
    application.add_handler(CommandHandler("source", source_command))
    application.add_handler(CommandHandler("delivery", delivery_command))
    application.add_handler(CommandHandler("paymentgroup", paymentgroup_command))
    application.add_handler(CommandHandler("paymentverification", paymentverification_command_handler))
    application.add_handler(CommandHandler("walletverification", walletverification_command_handler))
    application.add_handler(CommandHandler(["a", "A"], category_a_command))
    application.add_handler(CommandHandler(["b", "B"], category_b_command))
    application.add_handler(CommandHandler("category", category_check_command))
    application.add_handler(CommandHandler("removecategory", remove_category_command))
    application.add_handler(CommandHandler("approve", approve_order_command))
    application.add_handler(CommandHandler("reject", reject_order_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("removesource", removesource_command))
    application.add_handler(CommandHandler("removedelivery", removedelivery_command))
    application.add_handler(CommandHandler("resetgroups", resetgroups_command))

    # Register Multi-Loader Commands
    application.add_handler(CommandHandler("loaderadd", loaderadd_command))
    application.add_handler(CommandHandler("loaderlist", loaderlist_command))
    application.add_handler(CommandHandler("loaderremove", loaderremove_command))

    # Register User Management Commands
    application.add_handler(CommandHandler("user", user_command))
    application.add_handler(CommandHandler("users", users_command))

    # Register Core & Admin Commands
    application.add_handler(CommandHandler(["dashboard", "panel"], dashboard_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("delivered", delivered_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("order", order_info_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("resend", resend_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("restore", restore_command))
    application.add_handler(CommandHandler("exportprices", exportprices_command_handler))
    application.add_handler(CommandHandler("loaderexportprice", loaderexportprice_command_handler))
    application.add_handler(CommandHandler("updateprices", updateprices_command_handler))
    application.add_handler(CommandHandler("setclientprice", setclientprice_command_handler))
    application.add_handler(CommandHandler("setloaderprice", setloaderprice_command_handler))
    application.add_handler(CommandHandler("calculate", calculate_command_handler))
    application.add_handler(CommandHandler("total", running_total_command_handler))
    application.add_handler(CommandHandler("pay", pay_running_total_command_handler))
    application.add_handler(CommandHandler("undo", running_total_undo_command_handler))
    application.add_handler(CommandHandler("addprice", addprice_command_handler))
    application.add_handler(CommandHandler("subtractprice", subtractprice_command_handler))
    application.add_handler(CommandHandler("ledger", ledger_command_handler))
    application.add_handler(CommandHandler("todaytotal", todaytotal_command_handler))
    application.add_handler(CommandHandler("resetledger", resetledger_command_handler))
    application.add_handler(CommandHandler("cancelorder", client_cancel_command_handler))
    application.add_handler(CommandHandler("topup", topup_command_handler))
    application.add_handler(CommandHandler(["wallet", "balance"], wallet_command_handler))
    application.add_handler(CommandHandler("testbinance", testbinance_command_handler))

    # Operational Controls Handlers (Step 11)
    application.add_handler(CommandHandler("pendingorders", pendingorders_command_handler))
    application.add_handler(CommandHandler("assignloader", assignloader_command_handler))
    application.add_handler(CommandHandler("reassignloader", reassignloader_command_handler))
    application.add_handler(CommandHandler("myorders", myorders_command_handler))
    application.add_handler(CommandHandler("revieworders", revieworders_command_handler))
    application.add_handler(CommandHandler("completedorders", completedorders_command_handler))
    application.add_handler(CommandHandler("cancelledorders", cancelledorders_command_handler))
    application.add_handler(CommandHandler("failedorders", failedorders_command_handler))
    application.add_handler(CommandHandler("retryorder", retryorder_command_handler))

    # Register Interactive Callback Query Handlers
    application.add_handler(CallbackQueryHandler(operational_pagination_callback_handler, pattern="^op_"))
    application.add_handler(CallbackQueryHandler(client_cancellation_request_callback_handler, pattern="^cancel_req_"))
    application.add_handler(CallbackQueryHandler(duplicate_order_callback_handler, pattern="^dup_"))
    application.add_handler(CallbackQueryHandler(category_b_approval_callback_handler, pattern="^catb_"))
    application.add_handler(CallbackQueryHandler(price_callback_handler, pattern="^price_"))
    application.add_handler(CallbackQueryHandler(loader_issue_callback_handler, pattern="^loader_issue:"))
    application.add_handler(CallbackQueryHandler(customer_confirmation_callback_handler, pattern="^cust_confirm:"))
    application.add_handler(CallbackQueryHandler(redeliver_callback_handler, pattern="^redeliver_"))
    application.add_handler(CallbackQueryHandler(unknown_package_price_callback_handler, pattern="^add_unk_price:"))
    application.add_handler(CallbackQueryHandler(loader_pkg_toggle_callback_handler, pattern="^pkg_toggle:"))
    application.add_handler(CallbackQueryHandler(loader_pkg_confirm_callback_handler, pattern="^pkg_confirm:"))
    application.add_handler(CallbackQueryHandler(loader_pkg_cancel_callback_handler, pattern="^pkg_cancel:"))
    application.add_handler(CallbackQueryHandler(ledger_undo_callback_handler, pattern="^ledger_undo_"))
    application.add_handler(CallbackQueryHandler(calc_undo_callback_handler, pattern="^calc_undo_"))
    application.add_handler(CallbackQueryHandler(running_total_undo_callback_handler, pattern="^rt_undo_"))

    # Register manual_running_total_text_handler FIRST in group 0 for + / - numeric adjustments
    application.add_handler(
        MessageHandler(
            filters.Regex(r"^[\+\-]\d+(\.\d+)?$") & (~filters.COMMAND),
            manual_running_total_text_handler
        ),
        group=0
    )

    # Register admin_profit_code_completion_handler for post-delivery profit code submissions
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ProfitCodeInputFilter() & (~filters.COMMAND),
            admin_profit_code_completion_handler
        ),
        group=0
    )

    # Register loader_text_wizard_handler for interactive wizard text inputs
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            loader_text_wizard_handler
        ),
        group=0
    )

    # Register price_input_text_handler for reply messages
    application.add_handler(
        MessageHandler(
            filters.REPLY & filters.TEXT & (~filters.COMMAND),
            price_input_text_handler
        ),
        group=0
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            bulk_price_update_text_handler
        ),
        group=0
    )

    # Register Client Group Handler (Group 1 - Customer Orders)
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.PHOTO) & (~filters.COMMAND) & (~filters.UpdateType.EDITED_MESSAGE),
            source_group_handler
        ),
        group=1
    )

    # Register Client Group Edited Message Handler (Group 1 - Customer Message Edits)
    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & (~filters.COMMAND),
            edited_message_handler
        ),
        group=1
    )

    # Register Loader Group Handler (Group 2 - Loader Photos / Photo Documents / Text Replies like 'wrong')
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & (~filters.COMMAND),
            delivery_group_handler
        ),
        group=2
    )

    return application


async def run_services() -> None:
    """Runs Telegram Bot Polling and FastAPI Web Server concurrently in a single async event loop."""
    import uvicorn
    from web.app import app as fastapi_app

    application = build_application()

    # 1. Initialize and start Telegram Bot polling non-blockingly
    await application.initialize()
    await post_init(application)
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram Bot active and polling for updates.")

    # 2. Configure Uvicorn Web Server for Railway & local dashboard access
    port = Config.DASHBOARD_PORT
    uvicorn_config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False
    )
    server = uvicorn.Server(uvicorn_config)
    logger.info(f"Dashboard Web Server running on http://0.0.0.0:{port}")

    try:
        await server.serve()
    finally:
        logger.info("Shutting down Telegram Bot...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> None:
    """Entry point for launching both Telegram Bot and Dashboard."""
    if not Config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing! Please configure it in .env file or environment variables.")
        sys.exit(1)

    logger.info("Starting Telegram Email Image Delivery Bot & Live Dashboard...")
    asyncio.run(run_services())


if __name__ == "__main__":
    main()

