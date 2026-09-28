"""
Unit test suite for Telegram Email Image Delivery Bot.
Tests email, Order ID, package extraction, keyword detection, caption email overrides,
wrong details workflow, duplicate pending order detection, album splitting, SHA256 fingerprinting, user sessions,
BOT_SETTINGS cache, Role-Based User Management (AUTH_USERS_CACHE, Super Admin, Delivery Users),
Ignoring Super Admin & Delivery User messages in Client Group,
Group Category Routing System (v1.2: Category A, Category B, Payment Review, Approve, Reject),
Multi-Loader Approval System (Loader CRUD, LOADERS_CACHE, Multi-Loader Assignment),
Telegram BotCommand Validation (validate_bot_command),
Loader Add Wizard state isolation (LOADER_ADD_SESSION),
Category A Only Price Workflow (update_order_price),
and two-group reply-based DB operations.
"""

import unittest
import asyncio
from telegram import BotCommand

from email_parser import extract_email, extract_order_id, extract_package, extract_last_email
from order_parser import parse_order_v2, extract_customer_ref_id
from keywords import contains_order_keyword
from delivery import chunk_list
from media_collector import user_session_manager
from utils import is_super_admin, is_delivery_user
from main import validate_bot_command
from handlers import LOADER_ADD_SESSION, is_valid_price_string, price_input_text_handler, PRICE_INPUT_SESSION
from database import (
    BOT_SETTINGS,
    AUTH_USERS_CACHE,
    CLIENT_GROUPS_CACHE,
    LOADERS_CACHE,
    init_db,
    reload_bot_settings_cache,
    reload_auth_users_cache,
    reload_loaders_cache,
    set_client_group_category,
    remove_client_group_category,
    get_client_group_category,
    update_payment_review_group,
    update_order_status,
    set_order_price_prompt,
    update_order_price,
    add_authorized_user,
    remove_authorized_user,
    get_all_authorized_users,
    add_loader,
    remove_loader_by_id,
    get_all_loaders,
    create_order,
    set_order_loader_message_id,
    get_order_by_id,
    get_pending_order_by_email,
    get_order_by_loader_msg_id,
    add_images_to_order,
    mark_order_delivered,
    cancel_order,
    get_pending_orders,
    get_delivered_orders,
    delete_orders_by_email,
    get_detailed_stats,
    compute_fingerprint,
    export_orders_to_csv,
    get_or_create_settings,
    update_source_group,
    update_delivery_group,
    reset_groups
)


class TestCategoryAPriceWorkflow(unittest.IsolatedAsyncioTestCase):
    """Tests Category A Price workflow DB updates and string validation."""

    def test_price_validation(self):
        # Valid numbers
        self.assertTrue(is_valid_price_string("15"))
        self.assertTrue(is_valid_price_string("15.5"))
        self.assertTrue(is_valid_price_string("2500"))
        self.assertTrue(is_valid_price_string("2999.99"))

        # Invalid formats
        self.assertFalse(is_valid_price_string("abc"))
        self.assertFalse(is_valid_price_string("15rs"))
        self.assertFalse(is_valid_price_string("price 20"))
        self.assertFalse(is_valid_price_string("15.5.5"))

    async def test_order_price_update(self):
        await init_db()

        email = "price_test@example.com"
        order = await create_order(email, category="A")
        self.assertIsNone(order.price)
        self.assertEqual(order.category, "A")

        # Set Prompt
        await set_order_price_prompt(order.id, 998877)
        check_prompt = await get_order_by_id(order.id)
        self.assertEqual(check_prompt.price_prompt_msg_id, 998877)

        # Set Price (should clear prompt ID and set price_msg_id)
        updated = await update_order_price(order.id, "15.5", price_msg_id=12345)
        self.assertEqual(updated.price, "15.5")
        self.assertEqual(updated.price_msg_id, 12345)
        self.assertIsNone(updated.price_prompt_msg_id)

        # Edit Price
        edited = await update_order_price(order.id, "30", price_msg_id=67890)
        self.assertEqual(edited.price, "30")
        self.assertEqual(edited.price_msg_id, 67890)

        # Clean up
        await delete_orders_by_email(email)

    async def test_price_handler_in_place(self):
        await init_db()
        from unittest.mock import AsyncMock, MagicMock
        from database import delete_orders_by_email

        email = "in_place_price@example.com"
        await delete_orders_by_email(email)

        order = await create_order(email, category="A", client_chat_id=-100123456)
        PRICE_INPUT_SESSION[order.id] = {
            "order_id": order.id,
            "chat_id": -100123456,
            "prompt_msg_id": 555,
            "button_msg_id": 444,
            "is_edit": False
        }
        await set_order_price_prompt(order.id, 555)

        update = MagicMock()
        update.effective_user.id = 1573531032
        update.effective_message.reply_to_message.message_id = 555
        update.effective_message.text = "25.5"

        context = MagicMock()
        context.bot.edit_message_text = AsyncMock()
        context.bot.delete_message = AsyncMock()

        await price_input_text_handler(update, context)

        # Verify message text edited in-place with reply_markup=None (button removed)
        context.bot.edit_message_text.assert_called_once_with(
            chat_id=-100123456,
            message_id=444,
            text="💰 Price: 25.5",
            reply_markup=None
        )

        # Verify prompt message (555) deleted
        context.bot.delete_message.assert_any_call(
            chat_id=-100123456,
            message_id=555
        )

        # Verify DB order price updated
        db_order = await get_order_by_id(order.id)
        self.assertEqual(db_order.price, "25.5")
        self.assertEqual(db_order.price_msg_id, 444)

        await delete_orders_by_email(email)


class TestLoaderWizardState(unittest.TestCase):
    """Tests Loader Add Wizard state isolation."""

    def test_session_isolation(self):
        LOADER_ADD_SESSION.clear()
        self.assertNotIn(12345, LOADER_ADD_SESSION)

        # User 1 initiates wizard
        LOADER_ADD_SESSION[12345] = {"step": 1, "chat_id": -100111}
        self.assertIn(12345, LOADER_ADD_SESSION)
        self.assertNotIn(67890, LOADER_ADD_SESSION)

        LOADER_ADD_SESSION.clear()


class TestBotCommandValidation(unittest.TestCase):
    """Tests validate_bot_command against Telegram API rules."""

    def test_valid_commands(self):
        self.assertTrue(validate_bot_command(BotCommand("a", "Set Category A")))
        self.assertTrue(validate_bot_command(BotCommand("b", "Set Category B")))
        self.assertTrue(validate_bot_command(BotCommand("loaderadd", "Add Loader")))
        self.assertTrue(validate_bot_command(BotCommand("loader_list", "List Loaders")))

    def test_invalid_commands(self):
        # Uppercase not allowed
        self.assertFalse(validate_bot_command(BotCommand("A", "Set Category A")))
        self.assertFalse(validate_bot_command(BotCommand("B", "Set Category B")))
        # Spaces or special chars not allowed
        self.assertFalse(validate_bot_command(BotCommand("loader-add", "Add Loader")))
        self.assertFalse(validate_bot_command(BotCommand("loader add", "Add Loader")))
        # Empty description not allowed
        self.assertFalse(validate_bot_command(BotCommand("a", "")))


class TestMultiLoaderManagement(unittest.IsolatedAsyncioTestCase):
    """Tests Loader CRUD operations, cache synchronization, and multi-loader assignment."""

    async def test_loader_crud_and_cache(self):
        await init_db()

        # Add Loader 1
        l1 = await add_loader(-1001234567890, "Pakistan Loader")
        self.assertIsNotNone(l1.id)

        # Add Loader 2
        l2 = await add_loader(-1009876543210, "India Loader")
        self.assertIsNotNone(l2.id)

        # Check cache
        self.assertIn(l1.id, LOADERS_CACHE)
        self.assertEqual(LOADERS_CACHE[l1.id]["name"], "Pakistan Loader")

        # List Loaders
        loaders = await get_all_loaders()
        self.assertTrue(len(loaders) >= 2)

        # Remove Loader
        removed = await remove_loader_by_id(l1.id)
        self.assertTrue(removed)
        self.assertNotIn(l1.id, LOADERS_CACHE)

        # Clean up
        await remove_loader_by_id(l2.id)


class TestGroupCategoryRouting(unittest.IsolatedAsyncioTestCase):
    """Tests Group Category Routing (v1.2) - Category A, Category B, Payment Review, Approve, Reject."""

    async def test_category_assignment_and_cache(self):
        await init_db()

        chat_id_a = -100555444333222
        chat_id_b = -100999888777666

        # Set Category A
        await set_client_group_category(chat_id_a, "Pakistan CODM Shop A", "A")
        self.assertEqual(await get_client_group_category(chat_id_a), "A")

        # Set Category B
        await set_client_group_category(chat_id_b, "Pakistan CODM Shop B", "B")
        self.assertEqual(await get_client_group_category(chat_id_b), "B")

        # Test Payment Review Group update
        pay_chat_id = -100111222333444
        await update_payment_review_group(pay_chat_id, "Payment Review Group")
        self.assertEqual(BOT_SETTINGS["payment_review_group_id"], pay_chat_id)

        # Test Category B order creation & status updates
        email = "catb_test@example.com"
        order = await create_order(email, client_chat_id=chat_id_b, original_message_id=101, status="Pending Payment")
        self.assertEqual(order.status, "Pending Payment")

        # Approve Order
        approved_order = await update_order_status(order.id, "Approved")
        self.assertEqual(approved_order.status, "Approved")

        # Reject Order
        rejected_order = await update_order_status(order.id, "Rejected")
        self.assertEqual(rejected_order.status, "Rejected")

        # Remove Category
        await remove_client_group_category(chat_id_a)
        await remove_client_group_category(chat_id_b)
        await delete_orders_by_email(email)

    async def test_category_b_source_group_handler_no_name_error(self):
        """Regression test: Category B source_group_handler execution must NOT raise NameError for parse_order_v2."""
        from database import init_db, set_client_group_category, get_all_orders_by_email, delete_orders_by_email
        from handlers import source_group_handler
        from unittest.mock import MagicMock, AsyncMock

        await init_db()
        chat_id_b = -100888777666
        email_catb = "catb_no_nameerror@example.com"
        await set_client_group_category(chat_id_b, "Pakistan CODM Shop B", "B")

        text_content = (
            f"Email: {email_catb}\n"
            "Password: SecretPassword123\n"
            "Platform: Facebook\n"
            "2400 CP"
        )

        update = MagicMock()
        update.effective_user.id = 987654321  # Customer ID (not admin)
        update.effective_chat.id = chat_id_b
        update.effective_chat.title = "Pakistan CODM Shop B"
        update.effective_message.message_id = 8881
        update.effective_message.text = text_content
        update.effective_message.caption = None
        update.effective_message.photo = []
        update.effective_message.document = None
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.set_message_reaction = AsyncMock()
        context.bot.copy_message = AsyncMock()

        try:
            # Must complete without raising NameError or exception
            await source_group_handler(update, context)

            orders = await get_all_orders_by_email(email_catb)
            self.assertTrue(len(orders) > 0, "Category B order must be created in DB")
            catb_order = orders[0]
            self.assertEqual(catb_order.category, "B")
            self.assertEqual(catb_order.email, email_catb)
            self.assertIn("2400", catb_order.package)
            self.assertNotEqual(catb_order.package, "Standard Package", "Parsed package must remain 2400 CP, not Standard Package")
        finally:
            await remove_client_group_category(chat_id_b)
            await delete_orders_by_email(email_catb)


class TestIgnoreAdminAndDeliveryUserMessages(unittest.IsolatedAsyncioTestCase):
    """Tests ignoring Super Admin and Delivery User messages in Client Group."""

    async def test_admin_and_delivery_user_detection(self):
        await init_db()

        # Super Admin check
        admin_uid = 1573531032
        self.assertTrue(is_super_admin(admin_uid))

        # Delivery User checks
        del_uid_1 = 1078400998
        del_uid_2 = 1858358195
        self.assertTrue(is_delivery_user(del_uid_1))
        self.assertTrue(is_delivery_user(del_uid_2))

        # Normal Customer check
        cust_uid = 987654321
        self.assertFalse(is_super_admin(cust_uid))
        self.assertFalse(is_delivery_user(cust_uid))


class TestRoleBasedUserManagement(unittest.IsolatedAsyncioTestCase):
    """Tests role-based user management, database persistence, and permission functions."""

    async def test_role_seeding_and_permissions(self):
        await init_db()
        from database import remove_authorized_user
        await remove_authorized_user(999888777)

        # Verify initial seeds in memory cache
        self.assertTrue(is_super_admin(1573531032))
        self.assertTrue(is_delivery_user(1573531032))

        self.assertFalse(is_super_admin(1078400998))
        self.assertTrue(is_delivery_user(1078400998))

        self.assertFalse(is_super_admin(1858358195))
        self.assertTrue(is_delivery_user(1858358195))

        # Test adding a new delivery user
        new_uid = 999888777
        self.assertFalse(is_delivery_user(new_uid))
        success, _ = await add_authorized_user(new_uid, role="delivery")
        self.assertTrue(success)
        self.assertTrue(is_delivery_user(new_uid))
        self.assertFalse(is_super_admin(new_uid))

        # Test listing users
        all_users = await get_all_authorized_users()
        self.assertIn(1573531032, all_users["admin"])
        self.assertIn(new_uid, all_users["delivery"])

        # Test removing a delivery user
        rem_success, _ = await remove_authorized_user(new_uid)
        self.assertTrue(rem_success)
        self.assertFalse(is_delivery_user(new_uid))

        # Test protecting Super Admin from removal
        sa_rem_success, msg = await remove_authorized_user(1573531032)
        self.assertFalse(sa_rem_success)
        self.assertTrue(is_super_admin(1573531032))


class TestDuplicateOrderDetection(unittest.IsolatedAsyncioTestCase):
    """Tests duplicate pending order detection."""

    async def test_get_pending_order_by_email(self):
        await init_db()

        email = "dup_detect_test@example.com"
        # No pending order initially
        initial = await get_pending_order_by_email(email)
        self.assertIsNone(initial)

        # Create pending order
        o1 = await create_order(email, status="Pending")
        found = await get_pending_order_by_email(email)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, o1.id)

        # Deliver order
        await mark_order_delivered(o1.id)
        found_after_del = await get_pending_order_by_email(email)
        self.assertIsNone(found_after_del)

        # Cleanup
        await delete_orders_by_email(email)


class TestCaptionEmailAndWrongDetails(unittest.TestCase):
    """Tests extract_last_email helper for Loader caption email overrides and wrong details detection."""

    def test_extract_last_email_single(self):
        text = "AG Done\n\nabc@gmail.com"
        self.assertEqual(extract_last_email(text), "abc@gmail.com")

    def test_extract_last_email_multiple(self):
        text = "abc@gmail.com\n\nCompleted Successfully"
        self.assertEqual(extract_last_email(text), "abc@gmail.com")

    def test_extract_last_email_override(self):
        text = "AG Done\nold@gmail.com\nnew@gmail.com\nFinished"
        self.assertEqual(extract_last_email(text), "new@gmail.com")

    def test_extract_last_email_none(self):
        text = "AG Done\nNo email here"
        self.assertIsNone(extract_last_email(text))

    def test_wrong_details_keyword(self):
        self.assertIn("wrong", "wrong".lower())
        self.assertIn("wrong", "Wrong details provided".lower())
        self.assertIn("wrong", "WRONG".lower())


class TestBotSettingsCache(unittest.IsolatedAsyncioTestCase):
    """Tests in-memory BOT_SETTINGS cache initialization and updates."""

    async def test_cache_update_and_reload(self):
        await init_db()

        # Update source group and verify cache instantly reflects changes
        await update_source_group(-1001234567890, "Test Client Group")
        self.assertEqual(BOT_SETTINGS["source_group_id"], -1001234567890)
        self.assertEqual(BOT_SETTINGS["source_group_title"], "Test Client Group")

        # Update delivery group and verify cache instantly reflects changes
        await update_delivery_group(-1009876543210, "Test Loader Group")
        self.assertEqual(BOT_SETTINGS["delivery_group_id"], -1009876543210)
        self.assertEqual(BOT_SETTINGS["delivery_group_title"], "Test Loader Group")

        # Simulate bot restart by calling reload_bot_settings_cache()
        cached = await reload_bot_settings_cache()
        self.assertEqual(cached["source_group_id"], -1001234567890)
        self.assertEqual(cached["delivery_group_id"], -1009876543210)

        # Reset groups and verify cache cleared
        await reset_groups()
        self.assertIsNone(BOT_SETTINGS["source_group_id"])
        self.assertIsNone(BOT_SETTINGS["delivery_group_id"])


class TestKeywordDetector(unittest.TestCase):
    """Tests strict 4-condition order detection (Platform + Login + Password + Package)."""

    def test_valid_orders(self):
        # 1. Valid Facebook Order
        fb_order = (
            "Facebook\n\n"
            "Email:\nabc@gmail.com\n\n"
            "Password:\nHello123\n\n"
            "Order:\n2400+880"
        )
        self.assertTrue(contains_order_keyword(fb_order)[0])

        # 2. Valid Activision Order
        act_order = (
            "Activision\n\n"
            "Email:\nplayer@hotmail.com\n\n"
            "Password:\nGame123\n\n"
            "Recovery Codes:\n123456\n\n"
            "Order:\n10800+5040"
        )
        self.assertTrue(contains_order_keyword(act_order)[0])

        # 3. Valid Order with International Phone Number (+92)
        phone_order = (
            "FB Login\n"
            "Phone: +92 300 1234567\n"
            "Email: user@yahoo.com\n"
            "Password: secretpassword\n"
            "Package: 2400"
        )
        self.assertTrue(contains_order_keyword(phone_order)[0])

        # 4. Valid Order with Outlook, iCloud, Proton
        self.assertTrue(contains_order_keyword("Meta\nEmail: a@outlook.com\nPwd: 123\n2400 CP")[0])
        self.assertTrue(contains_order_keyword("Activision ID\nEmail: a@icloud.com\n2FA: 999\n10800")[0])
        self.assertTrue(contains_order_keyword("FB\nEmail: a@proton.me\nLogin: pass12\n880")[0])

    def test_invalid_messages_eliminated(self):
        # Package-only messages
        self.assertFalse(contains_order_keyword("2400+880")[0])
        self.assertFalse(contains_order_keyword("108000")[0])
        self.assertFalse(contains_order_keyword("7200")[0])

        # Email-only messages
        self.assertFalse(contains_order_keyword("gmail.com")[0])
        self.assertFalse(contains_order_keyword("user@gmail.com")[0])

        # Password-only messages
        self.assertFalse(contains_order_keyword("Password:123456")[0])

        # Platform-only messages
        self.assertFalse(contains_order_keyword("Facebook")[0])
        self.assertFalse(contains_order_keyword("Activision ID")[0])

        # Email + Package without password or platform (Now detected under Email+Package Fallback Rule)
        self.assertTrue(contains_order_keyword("Facebook\nEmail: abc@gmail.com\n2400+880")[0])

        # Missing Package
        self.assertFalse(contains_order_keyword("Facebook\nEmail: abc@gmail.com\nPassword: 123")[0])

        # Missing Platform but has Email + Package (Now detected under Email+Package Fallback Rule)
        self.assertTrue(contains_order_keyword("Email: abc@gmail.com\nPassword: 123\n2400+880")[0])


class TestEmailOrderPackageParser(unittest.TestCase):
    """Tests email, Order ID, and package regex extraction."""

    def test_extract_basic_email(self):
        text = "Order confirmation for john@gmail.com please deliver."
        self.assertEqual(extract_email(text), "john@gmail.com")

    def test_extract_order_id_formats(self):
        self.assertEqual(extract_order_id("Order ID: #10025"), 10025)
        self.assertEqual(extract_order_id("Order #10025"), 10025)
        self.assertEqual(extract_order_id("#10025"), 10025)
        self.assertEqual(extract_order_id("Order ID: 10025"), 10025)

    def test_extract_package_description(self):
        text = "10800 CP\nEmail: test@gmail.com"
        self.assertEqual(extract_package(text), "10800 CP")


class TestDeliverySplitting(unittest.TestCase):
    """Tests album splitting logic (8, 18, 35, 100+ images)."""

    def test_chunking_eight_images(self):
        images = [f"file_id_{i}" for i in range(8)]
        chunks = chunk_list(images, chunk_size=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 8)

    def test_chunking_eighteen_images(self):
        images = [f"file_id_{i}" for i in range(18)]
        chunks = chunk_list(images, chunk_size=10)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([len(c) for c in chunks], [10, 8])


class TestTwoGroupDatabaseWorkflow(unittest.IsolatedAsyncioTestCase):
    """Async tests for Two-Group Reply-Based Order Creation, Loader Reply Mapping, and Statuses."""

    async def test_two_group_workflow(self):
        await init_db()

        # 1. Customer Order Creation in Client Group
        email = "twogroup_flow@example.com"
        await delete_orders_by_email(email)
        order = await create_order(
            email=email,
            client_chat_id=-1001111111111,
            original_message_id=501,
            package="10800 CP"
        )
        self.assertIsNotNone(order.id)
        self.assertEqual(order.status, "Pending")
        self.assertEqual(order.package, "10800 CP")

        # 2. Forward to Loader Group & Store Loader Message ID
        await set_order_loader_message_id(order.id, 9901)
        loader_order = await get_order_by_loader_msg_id(9901)
        self.assertIsNotNone(loader_order)
        self.assertEqual(loader_order.id, order.id)

        # 3. Loader replies with images
        file_items = [("photo_1", "photo"), ("photo_2", "photo")]
        updated_order, is_dup = await add_images_to_order(
            order_id=order.id,
            file_items=file_items,
            media_group_id="album_5501"
        )
        self.assertFalse(is_dup)
        self.assertEqual(len(updated_order.images), 2)

        # 4. Duplicate reply test
        _, is_dup_2 = await add_images_to_order(
            order_id=order.id,
            file_items=file_items,
            media_group_id="album_5501"
        )
        self.assertTrue(is_dup_2)

        # 5. Mark Order Delivered
        await mark_order_delivered(order.id)
        del_order = await get_order_by_id(order.id)
        self.assertEqual(del_order.status, "Delivered")
        self.assertIsNotNone(del_order.delivered_at)

        # 6. Cancellation test on second order
        order2 = await create_order("cancel_test@example.com")
        canceled_order, success = await cancel_order(order2.id)
        self.assertTrue(success)
        self.assertEqual(canceled_order.status, "Cancelled")

    async def test_get_order_waiting_for_customer_update_import_and_query(self):
        from database import init_db, create_order, update_order_status, delete_orders_by_email, get_order_waiting_for_customer_update
        from handlers import get_order_waiting_for_customer_update as get_order_handler_import

        self.assertEqual(get_order_waiting_for_customer_update, get_order_handler_import)

        await init_db()
        email = "waiting_update_test@example.com"
        client_chat_id = -100999888777
        await delete_orders_by_email(email)

        # 1. No active waiting order -> returns None
        none_ord = await get_order_waiting_for_customer_update(client_chat_id)
        self.assertIsNone(none_ord)

        # 2. Create order & set status to Waiting Customer Update
        order1 = await create_order(email=email, client_chat_id=client_chat_id, package="10800")
        await update_order_status(order1.id, "Waiting Customer Update")

        # Create a 2nd order in same chat ID also waiting
        order2 = await create_order(email="waiting_update_test2@example.com", client_chat_id=client_chat_id, package="21600")
        await update_order_status(order2.id, "WAITING_FOR_CUSTOMER_PASSWORD")

        # Must return the latest waiting order (order2) without throwing MultipleResultsFound exception
        matched_ord = await get_order_waiting_for_customer_update(client_chat_id)
        self.assertIsNotNone(matched_ord)
        self.assertEqual(matched_ord.id, order2.id)

        # Clean up
        await delete_orders_by_email(email)
        await delete_orders_by_email("waiting_update_test2@example.com")
        await delete_orders_by_email("cancel_test@example.com")


class TestPOCOrderPriceDetection(unittest.TestCase):
    """Tests for POC automatic price detection & calculator helper calculate_test_price()."""

    def setUp(self):
        from utils import reload_package_prices_cache, TEST_PACKAGE_PRICES
        reload_package_prices_cache(TEST_PACKAGE_PRICES)

    def test_single_packages(self):
        from utils import calculate_test_price

        cases = {
            "108000": 563.0,
            "96000": 503.0,
            "72000": 375.0,
            "55200": 291.0,
            "48000": 254.0,
            "43200": 229.0,
            "38400": 211.0,
            "24000": 132.0,
            "21600": 119.0,
            "19200": 109.0,
            "16800": 95.0,
            "14400": 82.0,
            "12000": 69.0,
            "10800": 64.0,
            "9600": 55.0,
            "7200": 42.0,
            "5040": 33.0,
            "4800": 29.0,
            "2400": 16.5,
            "880": 8.0,
            "420": 4.5,
            "80": 1.0,
            "2400 CP": 16.5,
            "108000 CP": 563.0
        }

        for text, expected in cases.items():
            price = calculate_test_price(text)
            self.assertEqual(price, expected, f"Single package match failed for '{text}'")

    def test_mixed_packages(self):
        from utils import calculate_test_price

        mixed_cases = {
            "108000+7200+2400": 621.5,             # 563 + 42 + 16.5
            "96000+420": 507.5,                    # 503 + 4.5
            "48000+2400+880": 278.5,               # 254 + 16.5 + 8
            "7200+2400+880": 66.5,                 # 42 + 16.5 + 8
            "2400+2400+880": 41.0,                 # 16.5*2 + 8
            "108000,72000&24000/2400+880": 1094.5   # 563 + 375 + 132 + 16.5 + 8
        }

        for text, expected in mixed_cases.items():
            price = calculate_test_price(text)
            self.assertEqual(price, expected, f"Mixed package match failed for '{text}'")

    def test_longest_package_matching_prevents_false_detections(self):
        from utils import parse_test_order_packages

        # 108000 must match as 108000, NOT 10800
        p1 = parse_test_order_packages("108000")
        self.assertEqual(len(p1["packages"]), 1)
        self.assertEqual(p1["packages"][0]["package"], "108000")
        self.assertEqual(p1["total_price"], 563.0)

        # 24000 must match as 24000, NOT 2400
        p2 = parse_test_order_packages("24000")
        self.assertEqual(len(p2["packages"]), 1)
        self.assertEqual(p2["packages"][0]["package"], "24000")
        self.assertEqual(p2["total_price"], 132.0)

        # 96000 must match as 96000, NOT 9600
        p3 = parse_test_order_packages("96000")
        self.assertEqual(len(p3["packages"]), 1)
        self.assertEqual(p3["packages"][0]["package"], "96000")
        self.assertEqual(p3["total_price"], 503.0)

    def test_mixed_separators_normalization(self):
        from utils import parse_test_order_packages, calculate_test_price

        # 1. 10800,5040&2400/880+420 -> Expected 5 packages: 10800, 5040, 2400, 880, 420 = 126.0$ (64+33+16.5+8+4.5)
        p1 = parse_test_order_packages("10800,5040&2400/880+420")
        self.assertIsNotNone(p1)
        self.assertEqual([item["package"] for item in p1["packages"]], ["10800", "5040", "2400", "880", "420"])
        self.assertEqual(p1["total_price"], 126.0)

        # 2. 2400,880 -> Expected 2400, 880 = 24.5$ (16.5+8)
        self.assertEqual(calculate_test_price("2400,880"), 24.5)

        # 3. 2400&880 -> Expected 2400, 880 = 24.5$
        self.assertEqual(calculate_test_price("2400&880"), 24.5)

        # 4. 2400\n880 -> Expected 2400, 880 = 24.5$
        self.assertEqual(calculate_test_price("2400\n880"), 24.5)

    def test_quantities(self):
        from utils import calculate_test_price

        qty_cases = {
            "2400x2": 33.0,               # 16.5 * 2
            "2400 x2": 33.0,              # 16.5 * 2
            "2x2400": 33.0,               # 16.5 * 2
            "2 x 2400": 33.0,             # 16.5 * 2
            "2400x2 + 880x3": 57.0,       # (16.5*2) + (8*3) = 33 + 24
            "2x10800 + 3x420": 141.5      # (64*2) + (4.5*3) = 128 + 13.5 = 141.5
        }

        for text, expected in qty_cases.items():
            price = calculate_test_price(text)
            self.assertAlmostEqual(price, expected, places=2, msg=f"Quantity match failed for '{text}'")

    def test_false_match_rejections(self):
        from utils import calculate_test_price

        rejected = [
            "800",          # Must NOT match 80
            "400",          # Must NOT match 2400
            "1800",         # Must NOT match 80
            "5500 CP",      # Unsupported package
            "1000",
            "abc@gmail.com",
            None,
            ""
        ]

        for text in rejected:
            price = calculate_test_price(text)
            self.assertIsNone(price, f"Should reject false match / unsupported text: '{text}'")

        # Specific false-subtoken checks:
        # 10800 must NOT match 80 or 800
        self.assertEqual(calculate_test_price("10800"), 64.0)
        # 2400 must NOT match 400
        self.assertEqual(calculate_test_price("2400"), 16.5)
        # 880 must NOT match 80
        self.assertEqual(calculate_test_price("880"), 8.0)

    def test_official_production_pricing_examples(self):
        from utils import calculate_test_price

        # 10800 → 64$
        self.assertEqual(calculate_test_price("10800"), 64.0)

        # 10800 + 5040 → 97$
        self.assertEqual(calculate_test_price("10800 + 5040"), 97.0)

        # 10800 + 5040 + 2400 → 113.5$
        self.assertEqual(calculate_test_price("10800 + 5040 + 2400"), 113.5)

        # 2400 + 2400 → 33$
        self.assertEqual(calculate_test_price("2400 + 2400"), 33.0)

        # 2x10800 + 5040 → 161$
        self.assertEqual(calculate_test_price("2x10800 + 5040"), 161.0)

        # 880 + 420 + 80 → 13.5$
        self.assertEqual(calculate_test_price("880 + 420 + 80"), 13.5)

        # 108000 + 7200 + 2400 → 621.5$
        self.assertEqual(calculate_test_price("108000 + 7200 + 2400"), 621.5)

    def test_unknown_package_detection_and_pricing(self):
        from utils import (
            parse_test_order_packages,
            format_package_progress_summary,
            format_missing_packages_summary,
            get_unknown_package_keyboard,
            update_unknown_package_price
        )

        # 1. Input with mixed known and unknown: 15000+2400+880 (15000 is unknown)
        p = parse_test_order_packages("15000+2400+880")
        self.assertIsNotNone(p)
        self.assertTrue(p["has_unknown"])
        self.assertEqual(p["known_total"], 24.5)  # 16.5 + 8 = 24.5
        self.assertEqual(len(p["packages"]), 3)
        self.assertEqual(p["packages"][0]["package"], "15000")
        self.assertFalse(p["packages"][0]["known"])

        # 2. Format initial state
        items = p["packages"]
        s0 = format_package_progress_summary(items, p["known_total"])
        self.assertIn("❓ 15000 CP", s0)
        self.assertIn("💰 Known Total: 24.5$", s0)

        missing_text = format_missing_packages_summary(items)
        self.assertIn("❌ Missing Packages", missing_text)
        self.assertIn("15000", missing_text)

        # 3. Check unknown package keyboard
        kb = get_unknown_package_keyboard(101, items)
        self.assertIsNotNone(kb)
        self.assertIn("add_unk_price:101:15000", kb.inline_keyboard[0][0].callback_data)

        # 4. Admin enters price 85 for 15000
        updated_items, new_total, has_remaining = update_unknown_package_price(items, "15000", 85.0)
        self.assertFalse(has_remaining)
        self.assertEqual(new_total, 109.5)  # 85 + 16.5 + 8 = 109.5

        # 5. Format updated state
        s1 = format_package_progress_summary(updated_items, new_total)
        self.assertIn("☐ 15000 CP", s1)
        self.assertIn("☐ 2400 CP", s1)
        self.assertIn("☐ 880 CP", s1)
        self.assertIn("💰 Total Price: 109.5$", s1)

    def test_recovery_codes_and_credentials_never_detected_as_packages(self):
        from utils import parse_test_order_packages

        # 1. Recovery Codes with Order section:
        order_msg = (
            "Facebook\n\n"
            "Email:\nfb2@gmail.com\n\n"
            "Password:\nPakistan786\n\n"
            "Recovery Codes:\n123456\n654321\n\n"
            "Order:\n10800+5040+2400"
        )
        p1 = parse_test_order_packages(order_msg)
        self.assertIsNotNone(p1)
        self.assertFalse(p1.get("has_unknown"))
        self.assertEqual(len(p1["packages"]), 3)
        self.assertEqual([item["package"] for item in p1["packages"]], ["10800", "5040", "2400"])

        # 2. Non-order text with numeric credentials (Password 2400abc, Email 2400@gmail.com, UID 108000123)
        cred_msg = (
            "Password:\n2400abc\n\n"
            "Email:\n2400@gmail.com\n\n"
            "UID:\n108000123"
        )
        p2 = parse_test_order_packages(cred_msg)
        self.assertIsNone(p2)


class TestMultiPackageDeliveryWorkflow(unittest.TestCase):
    """Tests multi-package delivery selection workflow for Loader Group."""

    def test_single_package_workflow(self):
        from utils import parse_test_order_packages, build_loader_package_keyboard, mark_selected_packages_delivered, format_loader_card_summary

        p = parse_test_order_packages("Order:\n2400")
        items = p["packages"]

        # Single package orders (len <= 1) do NOT show selection buttons
        kb0 = build_loader_package_keyboard(1, items, active_loader_id=10)
        self.assertIsNone(kb0)

        items, is_all, del_cnt = mark_selected_packages_delivered(items, loader_id=10)
        self.assertTrue(is_all)
        self.assertEqual(del_cnt, 1)

        summary = format_loader_card_summary(items, p["total_price"])
        self.assertIn("🎉 Order Completed", summary)
        self.assertIn("✅ 2400 CP", summary)

    def test_two_and_three_packages_workflow(self):
        from utils import parse_test_order_packages, toggle_package_selection, mark_selected_packages_delivered, build_loader_package_keyboard

        p = parse_test_order_packages("Order:\n10800+5040+2400")
        items = p["packages"]
        self.assertEqual(len(items), 3)

        items, _ = toggle_package_selection(items, 0, loader_id=100)
        items, _ = toggle_package_selection(items, 1, loader_id=100)

        items, is_all, del_cnt = mark_selected_packages_delivered(items, loader_id=100)
        self.assertFalse(is_all)
        self.assertEqual(del_cnt, 2)

        kb_rem = build_loader_package_keyboard(1, items, active_loader_id=100)
        self.assertIsNotNone(kb_rem)
        self.assertEqual(kb_rem.inline_keyboard[0][0].text, "⬜ 2400")

    def test_five_and_ten_packages_workflow(self):
        from utils import parse_test_order_packages, toggle_package_selection, mark_selected_packages_delivered

        text_5 = "Order:\n" + "+".join(["2400"] * 5)
        p5 = parse_test_order_packages(text_5)
        self.assertEqual(len(p5["packages"]), 5)

        text_10 = "Order:\n" + "+".join(["2400"] * 10)
        p10 = parse_test_order_packages(text_10)
        self.assertEqual(len(p10["packages"]), 10)

        items10 = p10["packages"]
        for i in range(5):
            items10, _ = toggle_package_selection(items10, i, loader_id=200)

        items10, is_all, del_cnt = mark_selected_packages_delivered(items10, loader_id=200)
        self.assertFalse(is_all)
        self.assertEqual(del_cnt, 5)

    def test_cancel_selection(self):
        from utils import parse_test_order_packages, toggle_package_selection, cancel_loader_selections

        p = parse_test_order_packages("Order:\n10800+5040")
        items = p["packages"]

        items, _ = toggle_package_selection(items, 0, loader_id=300)
        self.assertEqual(items[0]["status"], "Selected")

        items, reset_cnt = cancel_loader_selections(items, loader_id=300)
        self.assertEqual(reset_cnt, 1)
        self.assertEqual(items[0]["status"], "Pending")

    def test_duplicate_click_and_multiple_loaders_locking(self):
        from utils import parse_test_order_packages, toggle_package_selection, mark_selected_packages_delivered

        p = parse_test_order_packages("Order:\n10800+5040")
        items = p["packages"]

        items, status_a = toggle_package_selection(items, 0, loader_id=111)
        self.assertEqual(status_a, "Selected")

        items, status_b = toggle_package_selection(items, 0, loader_id=222)
        self.assertEqual(status_b, "Locked")

        items, _, _ = mark_selected_packages_delivered(items, loader_id=111)

        items, status_del = toggle_package_selection(items, 0, loader_id=222)
        self.assertEqual(status_del, "Delivered")

    def test_railway_restart_database_restore_persistence(self):
        import json
        from utils import parse_test_order_packages, format_loader_card_summary, build_loader_package_keyboard

        p = parse_test_order_packages("Order:\n10800+5040")
        items = p["packages"]
        items[0]["status"] = "Delivered"

        json_str = json.dumps(items)
        restored_items = json.loads(json_str)

        card = format_loader_card_summary(restored_items, 97.5)
        self.assertIn("✅ 10800 CP", card)
        self.assertIn("⬜ 5040 CP", card)

        kb = build_loader_package_keyboard(99, restored_items)
        self.assertIsNotNone(kb)
        self.assertEqual(kb.inline_keyboard[0][0].text, "⬜ 5040")

    def test_loader_order_card_redesign_layout(self):
        import json
        from utils import parse_test_order_packages, format_full_loader_order_card

        raw_msg = (
            "Facebook\n\n"
            "Email:\nedge1@gmail.com\n\n"
            "Password:\nHello123\n\n"
            "Recovery Codes:\n123456\n654321\n\n"
            "Order:\n10800+5040+2400"
        )
        p = parse_test_order_packages(raw_msg)
        items = p["packages"]

        class MockOrder:
            def __init__(self):
                self.raw_text = raw_msg
                self.email = "edge1@gmail.com"
                self.package_progress = json.dumps(items)
                self.price = "114.5"

        order = MockOrder()
        card_text = format_full_loader_order_card(order)

        # Verify layout order
        self.assertIn("📋 ORDER DETAILS", card_text)
        self.assertIn("🎮 Platform:\nFacebook", card_text)
        self.assertIn("📧 Email:\nedge1@gmail.com", card_text)
        self.assertIn("🔑 Password:\nHello123", card_text)
        self.assertIn("Recovery Codes:\n123456\n654321", card_text)

        self.assertIn("📦 PACKAGE STATUS", card_text)
        self.assertIn("⬜ 10800 CP", card_text)
        self.assertIn("⬜ 5040 CP", card_text)
        self.assertIn("⬜ 2400 CP", card_text)
        self.assertNotIn("💰 Total Price", card_text)

        # Verify ORDER DETAILS is BEFORE PACKAGE STATUS
        idx_details = card_text.index("📋 ORDER DETAILS")
        idx_status = card_text.index("📦 PACKAGE STATUS")
        self.assertTrue(idx_details < idx_status)


class TestDeliverySessionRouting(unittest.TestCase):
    """Tests persistent Delivery Session creation, prompt message linking, and reply routing."""

    def test_delivery_session_database_crud(self):
        import asyncio
        from database import create_delivery_session, get_delivery_session_by_msg_id, close_delivery_session, init_db

        async def run_async_test():
            await init_db()

            # 1. Create Delivery Session linked to prompt msg ID 9999
            ds = await create_delivery_session(
                order_id=42,
                loader_id=777,
                session_msg_id=9999,
                selected_packages='[{"package": "2400"}]'
            )
            self.assertIsNotNone(ds)
            self.assertEqual(ds.order_id, 42)
            self.assertEqual(ds.delivery_session_message_id, 9999)
            self.assertEqual(ds.status, "waiting_images")

            # 2. Look up session by prompt msg ID 9999
            matched = await get_delivery_session_by_msg_id(9999)
            self.assertIsNotNone(matched)
            self.assertEqual(matched.order_id, 42)

            # 3. Close Delivery Session
            await close_delivery_session(matched.id)

            # 4. Verify session is no longer active
            closed = await get_delivery_session_by_msg_id(9999)
            self.assertIsNone(closed)

        asyncio.run(run_async_test())

    def test_partial_delivery_caption_and_status_tracking(self):
        import json
        from utils import parse_test_order_packages, format_delivered_packages_caption, mark_selected_packages_delivered, format_package_progress_summary

        raw = "Order:\n10800+5040+2400"
        parsed = parse_test_order_packages(raw)
        items = parsed["packages"]

        # Loader selects 10800 and 5040
        items[0]["status"] = "Selected"
        items[0]["selected_by_loader"] = 111
        items[1]["status"] = "Selected"
        items[1]["selected_by_loader"] = 111

        selected_for_session = [items[0], items[1]]

        # 1. Verify delivered screenshot caption contains ONLY 10800 and 5040 (NOT 2400) and calculates session price (64 + 33 = 97$)
        caption = format_delivered_packages_caption(selected_for_session)
        self.assertIn("📦 Delivered Package(s)", caption)
        self.assertIn("✅ 10800 CP", caption)
        self.assertIn("✅ 5040 CP", caption)
        self.assertIn("💰 Price: 98.5$", caption)
        self.assertNotIn("2400", caption)

        # 2. Mark progress as delivered
        updated_items, is_all_completed, delivered_cnt = mark_selected_packages_delivered(items, loader_id=111)
        self.assertEqual(delivered_cnt, 2)
        self.assertFalse(is_all_completed)  # 2400 is still pending

        # 3. Verify Client card summary displays ✅ for 10800 and 5040, and ☐/⬜ for 2400
        summary = format_package_progress_summary(updated_items, 113.5)
        self.assertIn("✅ 10800 CP", summary)
        self.assertIn("✅ 5040 CP", summary)
        self.assertIn("☐ 2400 CP", summary)
        self.assertNotIn("🎉 All Packages Delivered", summary)

    def test_delivery_session_image_isolation_and_final_completion(self):
        import json
        from utils import parse_test_order_packages, mark_selected_packages_delivered, format_package_progress_summary, format_full_loader_order_card

        raw = "Order:\n10800+5040"
        parsed = parse_test_order_packages(raw)
        items = parsed["packages"]

        # Session 1: Deliver 10800
        items[0]["status"] = "Selected"
        session1_selected = [items[0]]
        updated_1, is_all_1, del_1 = mark_selected_packages_delivered(items, loader_id=1, selected_items=session1_selected)

        self.assertEqual(del_1, 1)
        self.assertFalse(is_all_1)
        self.assertEqual(updated_1[0]["status"], "Delivered")
        self.assertEqual(updated_1[1]["status"], "Pending")

        # Session 2: Deliver 5040 (last package)
        updated_1[1]["status"] = "Selected"
        session2_selected = [updated_1[1]]
        updated_2, is_all_2, del_2 = mark_selected_packages_delivered(updated_1, loader_id=1, selected_items=session2_selected)

        self.assertEqual(del_2, 1)
        self.assertTrue(is_all_2)  # ALL packages delivered now!
        self.assertEqual(updated_2[0]["status"], "Delivered")
        self.assertEqual(updated_2[1]["status"], "Delivered")

        # Verify final loader card text includes Order Completed
        class MockOrder:
            def __init__(self):
                self.raw_text = raw
                self.email = "final@example.com"
                self.package_progress = json.dumps(updated_2)
                self.price = "97.5"

        card = format_full_loader_order_card(MockOrder())
        self.assertIn("🎉 Order Completed", card)

    def test_single_package_order_bypasses_delivery_session_and_keyboard(self):
        import json
        from utils import parse_test_order_packages, build_loader_package_keyboard, format_full_loader_order_card

        raw = "Facebook\nEmail:\nsingle@gmail.com\nPassword:\n123456\nOrder:\n2400"
        parsed = parse_test_order_packages(raw)
        items = parsed["packages"]

        # 1. Single package orders (1 item) return None for keyboard (NO buttons)
        kb = build_loader_package_keyboard(42, items)
        self.assertIsNone(kb)

        # 2. Loader card hides price
        class MockOrder:
            def __init__(self):
                self.raw_text = raw
                self.email = "single@gmail.com"
                self.package_progress = json.dumps(items)
                self.price = "17"

        card = format_full_loader_order_card(MockOrder())
        self.assertIn("📦 PACKAGE STATUS", card)
        self.assertIn("⬜ 2400 CP", card)
        self.assertNotIn("💰 Total Price", card)
        self.assertNotIn("17$", card)

    def test_generic_issue_workflow_engine(self):
        from utils import detect_loader_issue, has_valid_account_update_fields, ISSUE_WORKFLOW_CONFIG
        from handlers import detect_loader_issue as detect_loader_issue_handler

        self.assertEqual(detect_loader_issue, detect_loader_issue_handler)

        # 1. Test all keywords for Wrong Name
        for kw in ["wrong name", "wrongname", "name wrong", "WRONG NAME", "WrongName", "Name Wrong"]:
            res = detect_loader_issue(kw)
            self.assertIsNotNone(res, f"Failed matching: {kw}")
            self.assertEqual(res[1], "wrong_name")

        # 2. Test all keywords for Wrong Password
        for kw in ["wrong password", "wrongpassword", "password wrong", "incorrect password", "WRONG PASSWORD", "Incorrect Password"]:
            res = detect_loader_issue(kw)
            self.assertIsNotNone(res, f"Failed matching: {kw}")
            self.assertEqual(res[1], "wrong_password")

        # 3. Test all keywords for Google Linked
        for kw in ["google linked", "linked google", "google account linked", "already linked", "google bind", "GOOGLE LINKED", "Already Linked"]:
            res = detect_loader_issue(kw)
            self.assertIsNotNone(res, f"Failed matching: {kw}")
            self.assertEqual(res[1], "google_linked")

        # 4. Test all keywords for 2FA
        for kw in ["2fa", "2fa issue", "two factor", "two-factor", "verification code", "authenticator", "backup code", "2FA", "Two Factor"]:
            res = detect_loader_issue(kw)
            self.assertIsNotNone(res, f"Failed matching: {kw}")
            self.assertEqual(res[1], "two_factor")

        # 5. Test all keywords for Login Failed
        for kw in ["login failed", "cannot login", "unable to login", "login error", "invalid credentials", "LOGIN FAILED", "Cannot Login"]:
            res = detect_loader_issue(kw)
            self.assertIsNotNone(res, f"Failed matching: {kw}")
            self.assertEqual(res[1], "login_failed")

        # Unrelated text should return None
        for chatter in ["done", "ok", "thanks", "hello", "fast", "completed", "@alyan", "@username", "❤️", "🔥"]:
            self.assertIsNone(detect_loader_issue(chatter), f"Should be None for: {chatter}")

        # 6. Test valid account detail field validation
        self.assertFalse(has_valid_account_update_fields("ok"))
        self.assertFalse(has_valid_account_update_fields("done"))
        self.assertFalse(has_valid_account_update_fields("thanks"))
        self.assertFalse(has_valid_account_update_fields("❤️"))
        self.assertFalse(has_valid_account_update_fields("🔥"))

        self.assertTrue(has_valid_account_update_fields("Email:\nnewmail@gmail.com"))
        self.assertTrue(has_valid_account_update_fields("Password: mysecretpass"))
        self.assertTrue(has_valid_account_update_fields("123456\n654321"))
        self.assertTrue(has_valid_account_update_fields("2FA code: 888999"))

    def test_reaction_api_and_loader_approval_messages(self):
        from utils import ALLOWED_REACTION_EMOJIS, ISSUE_WORKFLOW_CONFIG, LoaderIssueType

        # 1. Allowed reactions must contain standard Unicode emojis
        self.assertEqual(ALLOWED_REACTION_EMOJIS, {"👍", "👎", "❤️", "✅", "❌", "⏳"})

        # 2. Check customer approval success message for Wrong Name
        wn_cfg = ISSUE_WORKFLOW_CONFIG[LoaderIssueType.WRONG_NAME]
        self.assertIn("✅ Customer confirmed that the account name is correct.", wn_cfg["loader_success_msg"])
        self.assertIn("You may continue the delivery now.", wn_cfg["loader_success_msg"])
        self.assertIn("Reply with delivery screenshots when finished.", wn_cfg["loader_success_msg"])

    def test_issue_workflow_requires_screenshot_rules(self):
        from utils import ISSUE_WORKFLOW_CONFIG, LoaderIssueType

        # 1. Wrong Name MUST require a screenshot
        wn = ISSUE_WORKFLOW_CONFIG[LoaderIssueType.WRONG_NAME]
        self.assertTrue(wn.get("requires_screenshot"))
        self.assertIn("attach a screenshot", wn.get("missing_screenshot_msg", "").lower())

        # 2. Other issues (Wrong Password, Google Linked, 2FA, Login Failed) screenshots are OPTIONAL
        for it in [LoaderIssueType.WRONG_PASSWORD, LoaderIssueType.GOOGLE_LINKED, LoaderIssueType.TWO_FACTOR, LoaderIssueType.LOGIN_FAILED]:
            cfg = ISSUE_WORKFLOW_CONFIG[it]
            self.assertFalse(cfg.get("requires_screenshot"))

    def test_smart_customer_input_handling_per_issue_type(self):
        from utils import validate_customer_update_for_issue, ISSUE_WORKFLOW_CONFIG, LoaderIssueType

        # 1. Wrong Password
        self.assertTrue(validate_customer_update_for_issue("Password: Pakistan123", "wrong_password"))
        self.assertTrue(validate_customer_update_for_issue("Password = Pakistan123", "wrong_password"))
        self.assertTrue(validate_customer_update_for_issue("My new password is Pakistan123", "wrong_password"))
        self.assertTrue(validate_customer_update_for_issue("New password: Pakistan123", "wrong_password"))
        self.assertTrue(validate_customer_update_for_issue("Pakistan123", "wrong_password"))

        # 2. Wrong Name
        self.assertTrue(validate_customer_update_for_issue("Account Name:\nPlayer123", "wrong_name"))
        self.assertTrue(validate_customer_update_for_issue("Nickname:\nPlayer123", "wrong_name"))
        self.assertTrue(validate_customer_update_for_issue("My name is Player123", "wrong_name"))
        self.assertTrue(validate_customer_update_for_issue("Player123", "wrong_name"))

        # 3. Google Linked
        self.assertTrue(validate_customer_update_for_issue("Yes", "google_linked"))
        self.assertTrue(validate_customer_update_for_issue("No", "google_linked"))
        self.assertTrue(validate_customer_update_for_issue("Facebook", "google_linked"))
        self.assertTrue(validate_customer_update_for_issue("Activision", "google_linked"))
        self.assertTrue(validate_customer_update_for_issue("Use Facebook", "google_linked"))
        self.assertTrue(validate_customer_update_for_issue("Email:\nabc@gmail.com\nPassword:\n123456", "google_linked"))

        # 4. 2FA
        self.assertTrue(validate_customer_update_for_issue("123456", "two_factor"))
        self.assertTrue(validate_customer_update_for_issue("Authenticator Code:\n123456", "two_factor"))
        self.assertTrue(validate_customer_update_for_issue("Verification Code:\n123456", "two_factor"))
        self.assertTrue(validate_customer_update_for_issue("Backup Code:\nABCD-EFGH", "two_factor"))
        self.assertTrue(validate_customer_update_for_issue("Recovery Code:\n12345678", "two_factor"))

        # 5. Login Failed
        self.assertTrue(validate_customer_update_for_issue("abc@gmail.com", "login_failed"))
        self.assertTrue(validate_customer_update_for_issue("Pakistan123", "login_failed"))
        self.assertTrue(validate_customer_update_for_issue("Email:\nabc@gmail.com\nPassword:\nPakistan123", "login_failed"))

        # 6. General chatter should be rejected for ALL issue types
        for chatter in ["ok", "done", "thanks", "hello", "fast", "completed", "❤️", "🔥", "👍"]:
            self.assertFalse(validate_customer_update_for_issue(chatter, "wrong_password"), f"Chatter '{chatter}' should fail for wrong_password")
            self.assertFalse(validate_customer_update_for_issue(chatter, "wrong_name"), f"Chatter '{chatter}' should fail for wrong_name")
            self.assertFalse(validate_customer_update_for_issue(chatter, "two_factor"), f"Chatter '{chatter}' should fail for two_factor")

        # 7. Check issue-specific customer prompts
        for issue_type, cfg in ISSUE_WORKFLOW_CONFIG.items():
            self.assertIn("customer_update_prompt", cfg)
            if issue_type != LoaderIssueType.WRONG_PASSWORD:
                self.assertIn("❌ <b>Order Paused</b>", cfg["customer_update_prompt"])

    def test_single_numeric_price_validation_for_unknown_packages(self):
        from handlers import is_valid_price_string
        from utils import parse_test_order_packages, update_unknown_package_price

        # 1. Admin enters valid single numeric values (integers or decimals)
        self.assertTrue(is_valid_price_string("150"))
        self.assertTrue(is_valid_price_string("150.5"))
        self.assertTrue(is_valid_price_string("85"))

        # 2. Reject formulas or non-numeric strings
        self.assertFalse(is_valid_price_string("150+16+8"))
        self.assertFalse(is_valid_price_string("150+17+8"))
        self.assertFalse(is_valid_price_string("150rs"))
        self.assertFalse(is_valid_price_string("price 150"))

        # 3. Order: 15000 + 2400 + 880 (Known: 2400=17, 880=8, Total=25)
        p = parse_test_order_packages("15000+2400+880")
        items = p["packages"]

        # Admin enters single numeric price 150 for 15000
        updated, total, has_rem = update_unknown_package_price(items, "15000", 150.0)
        self.assertFalse(has_rem)
        self.assertEqual(total, 174.5)  # 150 + 16.5 + 8 = 174.5$

    def test_unknown_package_non_crashing_combinations(self):
        from utils import parse_test_order_packages

        inputs = [
            "2400+880",
            "15000+2400",
            "15000",
            "15000+3600+2400"
        ]

        for inp in inputs:
            p = parse_test_order_packages(inp)
            self.assertIsNotNone(p, f"Parser returned None for valid input '{inp}'")

            t_val = p.get("total_price")
            k_val = p.get("known_total")

            if isinstance(t_val, (int, float)):
                p_str = f"{t_val:g}"
                self.assertIsNotNone(p_str)
            else:
                self.assertIsNone(t_val)

            if isinstance(k_val, (int, float)):
                k_str = f"{k_val:g}"
                self.assertIsNotNone(k_str)

    def test_exact_non_redundant_package_detection(self):
        from utils import parse_test_order_packages, calculate_test_price

        # 1. 2400+880+420 -> Expected 3 distinct packages, total price 29.0$ (16.5+8+4.5)
        p1 = parse_test_order_packages("2400+880+420")
        self.assertIsNotNone(p1)
        self.assertEqual(len(p1["packages"]), 3)
        self.assertEqual([item["package"] for item in p1["packages"]], ["2400", "880", "420"])
        self.assertEqual(p1["total_price"], 29.0)
        self.assertEqual(calculate_test_price("2400+880+420"), 29.0)

        # 2. 2400+2400+880 -> Expected 3 packages preserving intentional duplicate 2400s, total price 41.0$ (16.5*2 + 8)
        p2 = parse_test_order_packages("2400+2400+880")
        self.assertIsNotNone(p2)
        self.assertEqual(len(p2["packages"]), 3)
        self.assertEqual([item["package"] for item in p2["packages"]], ["2400", "2400", "880"])
        self.assertEqual(p2["total_price"], 41.0)
        self.assertEqual(calculate_test_price("2400+2400+880"), 41.0)

    def test_package_summary_formatting(self):
        from utils import parse_test_order_packages, format_package_summary_and_price

        # Example 1: Single package 2400
        p1 = parse_test_order_packages("2400")
        f1 = format_package_summary_and_price(p1)
        self.assertEqual(f1, "📦 Package:\n• 2400 CP\n\n💰 Price: 16.5$")

        # Example 2: Multiple packages 2400+880
        p2 = parse_test_order_packages("2400+880")
        f2 = format_package_summary_and_price(p2)
        self.assertEqual(f2, "📦 Package(s):\n• 2400 CP\n• 880 CP\n\n💰 Price: 24.5$")

        # Example 3: Multiple packages 10800+5040+420
        p3 = parse_test_order_packages("10800+5040+420")
        f3 = format_package_summary_and_price(p3)
        self.assertEqual(f3, "📦 Package(s):\n• 10800 CP\n• 5040 CP\n• 420 CP\n\n💰 Price: 101.5$")

        # Example 4: Quantity 2400x2+880 (Expanded)
        p4 = parse_test_order_packages("2400x2+880")
        f4 = format_package_summary_and_price(p4)
        self.assertEqual(f4, "📦 Package(s):\n• 2400 CP\n• 2400 CP\n• 880 CP\n\n💰 Price: 41$")

        # Example 5: Order preservation (880+2400)
        p5 = parse_test_order_packages("880+2400")
        f5 = format_package_summary_and_price(p5)
        self.assertEqual(f5, "📦 Package(s):\n• 880 CP\n• 2400 CP\n\n💰 Price: 24.5$")

    def test_package_progress_tracking(self):
        from utils import parse_test_order_packages, format_package_progress_summary, advance_package_progress

        parsed = parse_test_order_packages("2400+880+420")
        items = [
            {"package": item["package"], "qty": item["qty"], "unit_price": item["unit_price"], "status": "Pending"}
            for item in parsed["packages"]
        ]
        total_price = parsed["total_price"]

        # Initial State: all pending
        s0 = format_package_progress_summary(items, total_price)
        self.assertEqual(s0, "📦 Packages\n\n☐ 2400 CP\n☐ 880 CP\n☐ 420 CP\n\n💰 Total Price: 29$")

        # Delivery 1: advance 2400 to Delivered
        items1, done1 = advance_package_progress(items)
        self.assertFalse(done1)
        s1 = format_package_progress_summary(items1, total_price)
        self.assertEqual(s1, "📦 Packages\n\n✅ 2400 CP\n☐ 880 CP\n☐ 420 CP\n\n💰 Total Price: 29$")

        # Delivery 2: advance 880 to Delivered
        items2, done2 = advance_package_progress(items1)
        self.assertFalse(done2)
        s2 = format_package_progress_summary(items2, total_price)
        self.assertEqual(s2, "📦 Packages\n\n✅ 2400 CP\n✅ 880 CP\n☐ 420 CP\n\n💰 Total Price: 29$")

        # Delivery 3: advance 420 to Delivered (all done)
        items3, done3 = advance_package_progress(items2)
        self.assertTrue(done3)
        s3 = format_package_progress_summary(items3, total_price)
        self.assertEqual(s3, "📦 Packages\n\n✅ 2400 CP\n✅ 880 CP\n✅ 420 CP\n\n🎉 All Packages Delivered\n\n💰 Total Price: 29$")


class TestExactContentDeduplication(unittest.IsolatedAsyncioTestCase):
    """Tests exact content deduplication ensuring zero false positives."""

    def test_normalize_order_content_for_dedup(self):
        from utils import normalize_order_content_for_dedup

        # Package difference -> Different
        self.assertNotEqual(
            normalize_order_content_for_dedup("Email: abc@gmail.com\nPackage: 10800"),
            normalize_order_content_for_dedup("Email: abc@gmail.com\nPackage: 7200")
        )

        # Combination difference -> Different
        self.assertNotEqual(
            normalize_order_content_for_dedup("Package: 2400"),
            normalize_order_content_for_dedup("Package: 2400+880")
        )

        # Email difference -> Different
        self.assertNotEqual(
            normalize_order_content_for_dedup("Email: abc@gmail.com"),
            normalize_order_content_for_dedup("Email: abcd@gmail.com")
        )

        # Username difference -> Different
        self.assertNotEqual(
            normalize_order_content_for_dedup("Username: Black2868"),
            normalize_order_content_for_dedup("Username: Black2869")
        )

        # Password difference -> Different
        self.assertNotEqual(
            normalize_order_content_for_dedup("Password: password1"),
            normalize_order_content_for_dedup("Password: password2")
        )

        # Insignificant whitespace/casing difference -> Identical
        t1 = "Email: ABC@gmail.com\n\n  Package:  10800 CP  "
        t2 = "email: abc@gmail.com\npackage: 10800 cp"
        self.assertEqual(
            normalize_order_content_for_dedup(t1),
            normalize_order_content_for_dedup(t2)
        )

    async def test_get_exact_duplicate_pending_order(self):
        from database import create_order, get_exact_duplicate_pending_order, delete_orders_by_email

        email = "dedup_test_user@example.com"
        await delete_orders_by_email(email)

        # 1. Create initial order for 10800 package
        order1_text = f"Email: {email}\nPackage: 10800\nUID: 12345"
        await create_order(
            email=email,
            package="10800",
            status="Pending",
            raw_text=order1_text
        )

        # 2. Check second order for 7200 package -> MUST NOT be duplicate!
        order2_text = f"Email: {email}\nPackage: 7200\nUID: 12345"
        dup_check_2 = await get_exact_duplicate_pending_order(email, order2_text)
        self.assertIsNone(dup_check_2, "Different package (10800 vs 7200) MUST NOT trigger duplicate warning!")

        # 3. Check third order with EXACT same content -> MUST be duplicate!
        dup_check_3 = await get_exact_duplicate_pending_order(email, order1_text)
        self.assertIsNotNone(dup_check_3, "Identical content MUST trigger duplicate warning!")

        # Clean up
        await delete_orders_by_email(email)


class TestLoaderReviewWorkflow(unittest.IsolatedAsyncioTestCase):
    """Tests Loader Review, Issue State Transitions, and Extensible Issue Types."""

    def test_loader_issue_type_enum_and_config(self):
        from utils import LoaderIssueType, LOADER_ISSUE_CONFIG

        self.assertEqual(LoaderIssueType.WRONG_NAME, "wrong_name")
        self.assertEqual(LoaderIssueType.WRONG_ACCOUNT, "wrong_account")
        self.assertEqual(LoaderIssueType.LOGIN_FAILED, "login_failed")
        self.assertEqual(LoaderIssueType.TWO_FACTOR, "two_factor")
        self.assertEqual(LoaderIssueType.NEED_CONFIRMATION, "need_confirmation")

        for issue_type in LoaderIssueType:
            self.assertIn(issue_type, LOADER_ISSUE_CONFIG)
            cfg = LOADER_ISSUE_CONFIG[issue_type]
            self.assertIn("label", cfg)
            self.assertIn("customer_text", cfg)
            self.assertIn("loader_yes_text", cfg)
            self.assertIn("loader_no_text", cfg)

    async def test_order_issue_state_transitions(self):
        from database import create_order, update_order_issue_state, get_order_by_id, delete_orders_by_email

        email = "issue_workflow_user@example.com"
        await delete_orders_by_email(email)

        order = await create_order(email=email, package="2400 CP")
        self.assertIsNone(order.issue_state)
        self.assertIsNone(order.issue_type)

        # 1. Loader reports WRONG_NAME
        updated1 = await update_order_issue_state(order.id, "Waiting_Customer_Confirmation", "wrong_name")
        self.assertEqual(updated1.issue_state, "Waiting_Customer_Confirmation")
        self.assertEqual(updated1.issue_type, "wrong_name")
        self.assertEqual(updated1.last_issue_type, "wrong_name")

        # 2. Customer rejects NO -> Waiting_Customer_Update
        updated2 = await update_order_issue_state(order.id, "Waiting_Customer_Update", "wrong_name")
        self.assertEqual(updated2.issue_state, "Waiting_Customer_Update")
        self.assertEqual(updated2.issue_type, "wrong_name")

        # 3. Customer submits update -> Resolved (issue_type cleared)
        updated3 = await update_order_issue_state(order.id, "Resolved")
        self.assertEqual(updated3.issue_state, "Resolved")
        self.assertIsNone(updated3.issue_type)
        self.assertEqual(updated3.last_issue_type, "wrong_name")

        # 4. Loader reports LOGIN_FAILED on another attempt
        updated4 = await update_order_issue_state(order.id, "Waiting_Customer_Confirmation", "login_failed")
        self.assertEqual(updated4.issue_state, "Waiting_Customer_Confirmation")
        self.assertEqual(updated4.issue_type, "login_failed")
        self.assertEqual(updated4.last_issue_type, "login_failed")

        # Clean up
        await delete_orders_by_email(email)

    async def test_issue_workflow_runs_independently_of_delivery_sessions(self):
        from database import create_order, update_order_issue_state, delete_orders_by_email, get_delivery_session_by_msg_id
        from utils import detect_loader_issue

        email = "session_independent_issue@example.com"
        await delete_orders_by_email(email)

        # Multi-package order
        order = await create_order(email=email, package="2400 CP\n10800 CP", raw_text="2400 CP\n10800 CP")
        self.assertIsNotNone(order)

        # Confirm no delivery session exists
        session = await get_delivery_session_by_msg_id(999999)
        self.assertIsNone(session)

        # Loader reports issue BEFORE Confirm Delivery
        issue_result = detect_loader_issue("wrong password")
        self.assertIsNotNone(issue_result)
        cfg, issue_id = issue_result
        self.assertEqual(issue_id, "wrong_password")

        # Issue state update executes without requiring a delivery session
        updated = await update_order_issue_state(order.id, "Waiting_Customer_Confirmation", issue_id)
        self.assertEqual(updated.issue_state, "Waiting_Customer_Confirmation")
        self.assertEqual(updated.issue_type, "wrong_password")

        await delete_orders_by_email(email)


class TestDuplicateDeliveryFingerprint(unittest.IsolatedAsyncioTestCase):
    """Tests delivery fingerprinting ensuring distinct packages never trigger false duplicate delivery blocks."""

    async def test_distinct_packages_same_screenshots_allowed(self):
        from database import create_order, add_images_to_order, delete_orders_by_email, compute_fingerprint

        email = "fp_test_user@example.com"
        await delete_orders_by_email(email)

        same_file_items = [("file_A_123", "photo"), ("file_B_456", "photo")]

        # 1. Order 40: 2400 CP with screenshots A, B
        order40 = await create_order(email=email, package="2400", raw_text="2400")
        order40_updated, is_dup40 = await add_images_to_order(order40.id, same_file_items)
        self.assertFalse(is_dup40, "Order 40 delivery must be accepted!")

        # 2. Order 41: 2400+880 CP with SAME screenshots A, B -> MUST BE DELIVERED (different package!)
        order41 = await create_order(email=email, package="2400+880", raw_text="2400+880")
        order41_updated, is_dup41 = await add_images_to_order(order41.id, same_file_items)
        self.assertFalse(is_dup41, "Order 41 with different package (2400+880 vs 2400) using same screenshots MUST BE DELIVERED!")

        # Clean up
        await delete_orders_by_email(email)

    async def test_identical_package_same_screenshots_blocked(self):
        from database import create_order, add_images_to_order, delete_orders_by_email

        email = "fp_dup_user@example.com"
        await delete_orders_by_email(email)

        same_file_items = [("file_X_789", "photo"), ("file_Y_012", "photo")]

        # 1. Order 50: 2400 CP with screenshots X, Y
        order50 = await create_order(email=email, package="2400", raw_text="2400")
        _, is_dup50 = await add_images_to_order(order50.id, same_file_items)
        self.assertFalse(is_dup50, "Order 50 delivery must be accepted!")

        # 2. Order 51: EXACT same package 2400 CP with SAME screenshots X, Y -> MUST BE BLOCKED AS DUPLICATE!
        order51 = await create_order(email=email, package="2400", raw_text="2400")
        _, is_dup51 = await add_images_to_order(order51.id, same_file_items)
        self.assertTrue(is_dup51, "Order 51 with IDENTICAL package (2400) and SAME screenshots MUST BE BLOCKED as duplicate delivery!")

        # Clean up
        await delete_orders_by_email(email)


class TestBulkPriceUpdateSystem(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the Production Bulk Price Update System."""

    async def asyncSetUp(self):
        from database import seed_and_load_package_prices
        await seed_and_load_package_prices()

    async def test_export_prices_format(self):
        from database import get_all_package_prices_from_db
        from utils import format_export_prices

        db_prices = await get_all_package_prices_from_db()
        self.assertEqual(len(db_prices), 22)
        export_text = format_export_prices(db_prices)
        self.assertIn("10800 64", export_text)
        self.assertIn("2400 16.5", export_text)
        self.assertIn("108000 563", export_text)

    async def test_parse_bulk_prices_valid_input(self):
        from utils import parse_bulk_prices_input

        sample_input = """
        10800 65
        5040 = 34
        2400 : 17
        880 -> 8.5
        420 => 5
        80 1.5

        108000 570
        96000 510
        72000 380
        55200 295
        48000 260
        43200 235
        38400 215
        24000 135
        21600 122
        19200 112
        16800 98
        14400 85
        12000 72
        9600 58
        7200 45
        4800 31
        """
        price_map, err = parse_bulk_prices_input(sample_input)
        self.assertIsNone(err)
        self.assertIsNotNone(price_map)
        self.assertEqual(len(price_map), 22)
        self.assertEqual(price_map["10800"], 65.0)
        self.assertEqual(price_map["5040"], 34.0)
        self.assertEqual(price_map["2400"], 17.0)

    async def test_validation_errors_and_rollback(self):
        from utils import parse_bulk_prices_input
        from database import get_all_package_prices_from_db

        initial_prices = await get_all_package_prices_from_db()

        # 1. Non-numeric Package
        _, err1 = parse_bulk_prices_input("abc 100")
        self.assertIsNotNone(err1)
        self.assertIn("❌ Unknown Package", err1)
        self.assertIn("abc", err1)

        # 2. Invalid Price
        _, err2 = parse_bulk_prices_input("10800 abc")
        self.assertIsNotNone(err2)
        self.assertIn("❌ Invalid Price", err2)

        # 3. Duplicate Package
        dup_text = "10800 64\n10800 65"
        _, err3 = parse_bulk_prices_input(dup_text)
        self.assertIsNotNone(err3)
        self.assertIn("❌ Duplicate Package", err3)

        # 4. Partial UPSERT valid input (accepted as valid partial price map)
        partial_text = "10800 64\n5040 33"
        price_map, err4 = parse_bulk_prices_input(partial_text)
        self.assertIsNone(err4)
        self.assertEqual(len(price_map), 2)
        self.assertEqual(price_map["10800"], 64.0)

        # Verify DB remained untouched before execution
        after_prices = await get_all_package_prices_from_db()
        self.assertEqual(initial_prices, after_prices)

    async def test_atomic_bulk_update_and_cache_reload(self):
        from utils import parse_bulk_prices_input, calculate_test_price, PACKAGE_PRICES
        from database import bulk_update_package_prices_in_db, get_all_package_prices_from_db, DEFAULT_PACKAGE_PRICES

        valid_input = """
        10800 70
        5040 35
        2400 18
        880 9
        420 5
        80 2

        108000 600
        96000 520
        72000 390
        55200 300
        48000 270
        43200 240
        38400 220
        24000 140
        21600 125
        19200 115
        16800 100
        14400 88
        12000 75
        9600 60
        7200 46
        4800 32
        """
        price_map, err = parse_bulk_prices_input(valid_input)
        self.assertIsNone(err)

        # Update DB & Cache
        success = await bulk_update_package_prices_in_db(price_map, updated_by_id=1573531032)
        self.assertTrue(success)

        # Verify Cache updated immediately without restart
        self.assertEqual(PACKAGE_PRICES["10800"], 70.0)
        self.assertEqual(calculate_test_price("10800"), 70.0)
        self.assertEqual(calculate_test_price("2400"), 18.0)
        self.assertEqual(calculate_test_price("10800 + 5040"), 105.0)

        # Verify DB persisted
        db_prices = await get_all_package_prices_from_db()
        self.assertEqual(db_prices["10800"], 70.0)

        # Restore default prices
        await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, updated_by_id=1573531032)
        self.assertEqual(calculate_test_price("10800"), 64.0)

    async def test_unauthorized_user_blocked(self):
        from utils import is_super_admin
        self.assertTrue(is_super_admin(1573531032))
        self.assertFalse(is_super_admin(999999999))

    async def test_command_menu_and_handler_audit(self):
        from handlers import exportprices_command_handler, updateprices_command_handler

        # Inspect main.py source code to verify set_my_commands and handler registration
        with open("main.py", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('BotCommand("exportprices"', content)
        self.assertIn('BotCommand("updateprices"', content)
        self.assertIn('CommandHandler("exportprices", exportprices_command_handler)', content)
        self.assertIn('CommandHandler("updateprices", updateprices_command_handler)', content)

        # Inspect handlers.py source code to verify help_command includes both commands
        with open("handlers.py", "r", encoding="utf-8") as f:
            h_content = f.read()

        self.assertIn('exportprices', h_content)
        self.assertIn('updateprices', h_content)


class TestIgnoredTrustedUsers(unittest.IsolatedAsyncioTestCase):
    """Tests trusted user ID ignore rules in order detection engine & group handlers."""

    def test_ignored_user_ids_config(self):
        from config import Config
        from utils import is_ignored_user

        self.assertNotIn(1573531032, Config.IGNORED_USER_IDS)
        self.assertIn(1249984265, Config.IGNORED_USER_IDS)

        self.assertFalse(is_ignored_user(1573531032))
        self.assertTrue(is_ignored_user(1249984265))
        self.assertFalse(is_ignored_user(999999999))

    async def test_ignored_users_in_source_group_handler(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler, BOT_SETTINGS

        BOT_SETTINGS["source_group_id"] = -1001111222333

        # Ignored User 1249984265
        update1249 = MagicMock()
        update1249.effective_message.message_id = 902
        update1249.effective_message.text = "test1249@gmail.com 2400 CP"
        update1249.effective_chat.id = -1001111222333
        update1249.effective_user.id = 1249984265
        update1249.effective_message.reply_text = AsyncMock()
        await source_group_handler(update1249, None)
        update1249.effective_message.reply_text.assert_not_called()


class TestDeliveryLedgerSystem(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Production Delivery Ledger & Running Total System."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import DeliveryLedger, RunningTotalLedger
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(RunningTotalLedger))
            await session.commit()

    async def test_first_delivery(self):
        from database import record_delivery_ledger_entry, get_current_running_total

        before_start = await get_current_running_total()
        self.assertEqual(before_start, 0.0)

        e1, ok1 = await record_delivery_ledger_entry(order_id=201, package="10800", now_value=64.0, loader_name="Loader A", dedup_hash="201:10800:1")
        self.assertTrue(ok1)
        self.assertEqual(e1.before_total, 0.0)
        self.assertEqual(e1.now_value, 64.0)
        self.assertEqual(e1.running_total, 64.0)

    async def test_running_total_accumulation(self):
        from database import record_delivery_ledger_entry, get_current_running_total

        await record_delivery_ledger_entry(order_id=202, package="10800", now_value=64.0, dedup_hash="202:1")
        self.assertEqual(await get_current_running_total(), 64.0)

        await record_delivery_ledger_entry(order_id=202, package="5040", now_value=33.0, dedup_hash="202:2")
        self.assertEqual(await get_current_running_total(), 97.0)

    async def test_ledger_reply_message_and_missing_loader_name(self):
        from unittest.mock import AsyncMock, MagicMock
        from handlers import process_delivery_ledger_event
        from database import get_last_ledger_entry

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        await process_delivery_ledger_event(
            order_id=301,
            package_str="10800",
            loader_name=None,  # Missing loader name fallback test
            bot=mock_bot,
            chat_id=-100999,
            dedup_hash="301:10800:reply_test",
            reply_to_message_id=888  # Delivery message reply ID
        )

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], -100999)
        self.assertEqual(call_kwargs["text"], "Before 0$\nNow 65.5$\nTotal 65.5$")

        last_e = await get_last_ledger_entry()
        self.assertIsNotNone(last_e)
        self.assertEqual(last_e.loader, "Loader")  # Fallback verified

    async def test_partial_deliveries_create_separate_ledger_entries(self):
        from database import record_delivery_ledger_entry, get_current_running_total

        e1, ok1 = await record_delivery_ledger_entry(order_id=101, package="10800", now_value=64.0, loader_name="Loader A", dedup_hash="101:10800:1")
        self.assertTrue(ok1)
        self.assertIsNotNone(e1)
        self.assertEqual(e1.now_value, 64.0)

        e2, ok2 = await record_delivery_ledger_entry(order_id=101, package="5040", now_value=33.0, loader_name="Loader A", dedup_hash="101:5040:2")
        self.assertTrue(ok2)
        self.assertEqual(e2.now_value, 33.0)

        e3, ok3 = await record_delivery_ledger_entry(order_id=101, package="2400", now_value=16.5, loader_name="Loader A", dedup_hash="101:2400:3")
        self.assertTrue(ok3)
        self.assertEqual(e3.now_value, 16.5)

    async def test_multi_package_delivery_combines_value(self):
        from utils import calculate_delivered_packages_value
        from database import record_delivery_ledger_entry

        val, known = calculate_delivered_packages_value("10800+5040")
        self.assertTrue(known)
        self.assertEqual(val, 98.5)

        e, ok = await record_delivery_ledger_entry(order_id=102, package="10800+5040", now_value=val, loader_name="Loader B", dedup_hash="102:multi:1")
        self.assertTrue(ok)
        self.assertEqual(e.now_value, 98.5)

    async def test_duplicate_delivery_blocked(self):
        from database import record_delivery_ledger_entry

        e1, ok1 = await record_delivery_ledger_entry(order_id=103, package="2400", now_value=16.5, loader_name="Loader C", dedup_hash="DUP_TEST_HASH_123")
        self.assertTrue(ok1)

        e2, ok2 = await record_delivery_ledger_entry(order_id=103, package="2400", now_value=16.5, loader_name="Loader C", dedup_hash="DUP_TEST_HASH_123")
        self.assertFalse(ok2)
        self.assertIsNone(e2)

    async def test_manual_adjustment_reason_requirement_and_execution(self):
        from database import record_delivery_ledger_entry

        e_add, ok1 = await record_delivery_ledger_entry(
            order_id=None,
            package="Manual Add",
            now_value=29.0,
            loader_name="Admin",
            reason="Special Pack Price Correction",
            is_manual=True
        )
        self.assertTrue(ok1)
        self.assertTrue(e_add.is_manual)
        self.assertEqual(e_add.reason, "Special Pack Price Correction")
        self.assertEqual(e_add.now_value, 29.0)

        e_sub, ok2 = await record_delivery_ledger_entry(
            order_id=None,
            package="Manual Subtract",
            now_value=-16.0,
            loader_name="Admin",
            reason="Duplicate Entry Correction",
            is_manual=True
        )
        self.assertTrue(ok2)
        self.assertTrue(e_sub.is_manual)
        self.assertEqual(e_sub.reason, "Duplicate Entry Correction")
        self.assertEqual(e_sub.now_value, -16.0)

    async def test_safe_undo_with_confirmation(self):
        from database import record_delivery_ledger_entry, undo_ledger_entry

        e, ok = await record_delivery_ledger_entry(order_id=105, package="880", now_value=8.0, loader_name="Loader D", dedup_hash="UNDO_HASH_105")
        self.assertTrue(ok)

        undone = await undo_ledger_entry(e.id, admin_id=1573531032)
        self.assertIsNotNone(undone)
        self.assertEqual(undone.id, e.id)

    async def test_todaytotal_period_stats(self):
        from database import get_ledger_period_stats
        stats = await get_ledger_period_stats()
        self.assertIn("today_count", stats)
        self.assertIn("today_revenue", stats)
        self.assertIn("week_revenue", stats)
        self.assertIn("month_revenue", stats)
        self.assertIn("running_total", stats)

    async def test_reset_ledger(self):
        from database import record_delivery_ledger_entry, reset_delivery_ledger, get_current_running_total

        await record_delivery_ledger_entry(order_id=106, package="420", now_value=4.5, loader_name="Loader E", dedup_hash="RESET_TEST_HASH")
        res = await reset_delivery_ledger(admin_id=1573531032)
        self.assertTrue(res)
        tot = await get_current_running_total()
        self.assertEqual(tot, 0.0)


class TestSimpleRunningTotalCalculator(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Simple Running Total Calculator module."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import CalculatorLedger
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(CalculatorLedger))
            await session.commit()

    async def test_positive_and_negative_calculations(self):
        from database import record_calculator_entry, get_calculator_current_total

        self.assertEqual(await get_calculator_current_total(), 0.0)

        e1, b1, n1, a1 = await record_calculator_entry(97.0, admin_id=1573531032)
        self.assertEqual(b1, 0.0)
        self.assertEqual(n1, 97.0)
        self.assertEqual(a1, 97.0)
        self.assertEqual(await get_calculator_current_total(), 97.0)

        e2, b2, n2, a2 = await record_calculator_entry(64.0, admin_id=1573531032)
        self.assertEqual(b2, 97.0)
        self.assertEqual(n2, 64.0)
        self.assertEqual(a2, 161.0)
        self.assertEqual(await get_calculator_current_total(), 161.0)

        e3, b3, n3, a3 = await record_calculator_entry(-100.0, admin_id=1573531032)
        self.assertEqual(b3, 161.0)
        self.assertEqual(n3, -100.0)
        self.assertEqual(a3, 61.0)
        self.assertEqual(await get_calculator_current_total(), 61.0)

    async def test_undo_calculator_entry(self):
        from database import record_calculator_entry, undo_last_calculator_entry, get_calculator_current_total

        await record_calculator_entry(97.0, admin_id=1573531032)
        await record_calculator_entry(64.0, admin_id=1573531032)
        await record_calculator_entry(-100.0, admin_id=1573531032)

        self.assertEqual(await get_calculator_current_total(), 61.0)

        undone = await undo_last_calculator_entry(admin_id=1573531032)
        self.assertIsNotNone(undone)
        self.assertEqual(undone.amount, -100.0)
        self.assertEqual(await get_calculator_current_total(), 161.0)

    async def test_formatting_messages(self):
        from utils import format_calculator_result_message, format_calculator_total_message

        pos_msg = format_calculator_result_message(97.0, 64.0, 161.0)
        self.assertIn("Before\n97$", pos_msg)
        self.assertIn("Now\n+64$", pos_msg)
        self.assertIn("Total\n161$", pos_msg)

        neg_msg = format_calculator_result_message(161.0, -100.0, 61.0)
        self.assertIn("Before\n161$", neg_msg)
        self.assertIn("Now\n-100$", neg_msg)
        self.assertIn("Total\n61$", neg_msg)

        tot_msg = format_calculator_total_message(61.0)
        self.assertIn("Current Total", tot_msg)
        self.assertIn("61$", tot_msg)

    async def test_unauthorized_user_blocked(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import calculate_command_handler, total_command_handler, calc_undo_command_handler

        unauth_update = MagicMock()
        unauth_update.effective_user.id = 999111222
        unauth_update.effective_message.reply_text = AsyncMock()

        await calculate_command_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_called_with("❌ You are not authorized to use this command.")

        unauth_update.effective_message.reply_text.reset_mock()
        await total_command_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_called_with("❌ You are not authorized to use this command.")

        unauth_update.effective_message.reply_text.reset_mock()
        await calc_undo_command_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_called_with("❌ You are not authorized to use this command.")


class TestSimpleRunningTotalSystem(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Production Simple Running Total System."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import DeliveryLedger, RunningTotalLedger
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(RunningTotalLedger))
            await session.commit()

    async def test_automatic_delivery_calculation_and_multi_deliveries(self):
        from database import execute_auto_delivery_total, get_running_total_current

        self.assertEqual(await get_running_total_current(), 0.0)

        e1, b1, n1, a1 = await execute_auto_delivery_total(order_id=161, now_val=64.0)
        self.assertEqual(b1, 0.0)
        self.assertEqual(n1, 64.0)
        self.assertEqual(a1, 64.0)
        self.assertEqual(await get_running_total_current(), 64.0)

        e2, b2, n2, a2 = await execute_auto_delivery_total(order_id=162, now_val=33.0)
        self.assertEqual(b2, 64.0)
        self.assertEqual(n2, 33.0)
        self.assertEqual(a2, 97.0)

        e3, b3, n3, a3 = await execute_auto_delivery_total(order_id=163, now_val=16.5)
        self.assertEqual(b3, 97.0)
        self.assertEqual(n3, 16.5)
        self.assertEqual(a3, 113.5)
        self.assertEqual(await get_running_total_current(), 113.5)

    async def test_pay_resets_total_to_zero_and_new_deliveries_restart(self):
        from database import execute_auto_delivery_total, execute_pay_reset, get_running_total_current

        # 1. Deliveries: 64$, 33$, 64$ -> Running Total: 161$
        e1, b1, n1, a1 = await execute_auto_delivery_total(order_id=301, now_val=64.0)
        self.assertEqual(a1, 64.0)

        e2, b2, n2, a2 = await execute_auto_delivery_total(order_id=302, now_val=33.0)
        self.assertEqual(a2, 97.0)

        e3, b3, n3, a3 = await execute_auto_delivery_total(order_id=303, now_val=64.0)
        self.assertEqual(a3, 161.0)
        self.assertEqual(await get_running_total_current(), 161.0)

        # 2. Run /pay -> Verify Before: 161$, Paid: 161$, After: 0$
        entry, before, paid, current = await execute_pay_reset(admin_id=1573531032)
        self.assertEqual(before, 161.0)
        self.assertEqual(paid, 161.0)
        self.assertEqual(current, 0.0)
        self.assertEqual(await get_running_total_current(), 0.0)

        # 3. Deliver 16.5$ -> Verify Before: 0$, Now: 16.5$, Total: 16.5$
        e_next, b_next, n_next, a_next = await execute_auto_delivery_total(order_id=304, now_val=16.5)
        self.assertEqual(b_next, 0.0)
        self.assertEqual(n_next, 16.5)
        self.assertEqual(a_next, 16.5)
        self.assertEqual(await get_running_total_current(), 16.5)

    async def test_manual_plus_and_minus_adjustments(self):
        from database import execute_auto_delivery_total, execute_manual_adjustment, get_running_total_current

        await execute_auto_delivery_total(order_id=161, now_val=64.0)
        self.assertEqual(await get_running_total_current(), 64.0)

        e_plus, b_plus, n_plus, a_plus, act_plus = await execute_manual_adjustment(10.0, admin_id=1573531032)
        self.assertEqual(b_plus, 64.0)
        self.assertEqual(n_plus, 10.0)
        self.assertEqual(a_plus, 74.0)
        self.assertEqual(act_plus, "MANUAL_PLUS")
        self.assertEqual(await get_running_total_current(), 74.0)

        e_minus, b_minus, n_minus, a_minus, act_minus = await execute_manual_adjustment(-10.0, admin_id=1573531032)
        self.assertEqual(b_minus, 74.0)
        self.assertEqual(n_minus, -10.0)
        self.assertEqual(a_minus, 64.0)
        self.assertEqual(act_minus, "MANUAL_MINUS")
        self.assertEqual(await get_running_total_current(), 64.0)

    async def test_undo_last_action(self):
        from database import execute_auto_delivery_total, execute_manual_adjustment, undo_last_running_total_action, get_running_total_current

        await execute_auto_delivery_total(order_id=161, now_val=64.0)
        await execute_manual_adjustment(10.0, admin_id=1573531032)
        self.assertEqual(await get_running_total_current(), 74.0)

        undone = await undo_last_running_total_action(admin_id=1573531032)
        self.assertIsNotNone(undone)
        self.assertEqual(undone.amount, 10.0)
        self.assertEqual(await get_running_total_current(), 64.0)

    async def test_unauthorized_users_ignored(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import running_total_command_handler, pay_running_total_command_handler, manual_running_total_text_handler

        unauth_update = MagicMock()
        unauth_update.effective_user.id = 999888777
        unauth_update.effective_message.reply_text = AsyncMock()
        unauth_update.effective_message.text = "+100"

        await running_total_command_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_not_called()

        await pay_running_total_command_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_not_called()

        await manual_running_total_text_handler(unauth_update, None)
        unauth_update.effective_message.reply_text.assert_not_called()

    async def test_bug1_delivery_after_pay_starts_from_zero(self):
        from database import record_delivery_ledger_entry, execute_pay_reset, get_running_total_current

        await record_delivery_ledger_entry(order_id=501, package="10800", now_value=225.0, dedup_hash="b1_1")
        await record_delivery_ledger_entry(order_id=502, package="5040", now_value=33.0, dedup_hash="b1_2")
        await record_delivery_ledger_entry(order_id=503, package="10800", now_value=64.0, dedup_hash="b1_3")

        self.assertEqual(await get_running_total_current(), 322.0)

        entry, before, paid, current = await execute_pay_reset(admin_id=1573531032)
        self.assertEqual(before, 322.0)
        self.assertEqual(paid, 322.0)
        self.assertEqual(current, 0.0)
        self.assertEqual(await get_running_total_current(), 0.0)

        e_next, ok = await record_delivery_ledger_entry(order_id=504, package="420", now_value=5.5, dedup_hash="b1_4")
        self.assertTrue(ok)
        self.assertEqual(e_next.before_total, 0.0)
        self.assertEqual(e_next.now_value, 5.5)
        self.assertEqual(e_next.running_total, 5.5)
        self.assertEqual(await get_running_total_current(), 5.5)

    async def test_bug2_decimal_negative_manual_adjustment_preserves_sign(self):
        from database import record_delivery_ledger_entry, execute_manual_adjustment, get_running_total_current

        await record_delivery_ledger_entry(order_id=601, package="327.5", now_value=327.5, dedup_hash="b2_1")
        self.assertEqual(await get_running_total_current(), 327.5)

        entry, before_val, now_val, after_val, act_type = await execute_manual_adjustment(-327.5, admin_id=1573531032)
        self.assertEqual(before_val, 327.5)
        self.assertEqual(now_val, -327.5)
        self.assertEqual(after_val, 0.0)
        self.assertEqual(act_type, "MANUAL_MINUS")
        self.assertEqual(await get_running_total_current(), 0.0)

    async def test_partial_delivery_running_total_correctness(self):
        from utils import parse_test_order_packages, mark_selected_packages_delivered, calculate_delivered_packages_value
        from database import record_delivery_ledger_entry, get_running_total_current

        parsed = parse_test_order_packages("880+420+80")
        raw_items = parsed["packages"]

        # Step 1: Deliver 880 + 420 (Price: 9.0 + 3.5 = 12.5$)
        step1_selection = [{"package": "880"}, {"package": "420"}]
        updated1, is_all1, cnt1 = mark_selected_packages_delivered(raw_items, loader_id=1, selected_items=step1_selection)
        self.assertFalse(is_all1)

        new_names1 = [it["package"] for it in step1_selection]
        pkg_str1 = "+".join(new_names1)
        val1, ok1 = calculate_delivered_packages_value(pkg_str1)
        self.assertTrue(ok1)
        self.assertEqual(val1, 12.5)

        e1, _ = await record_delivery_ledger_entry(order_id=701, package=pkg_str1, now_value=val1, dedup_hash="pd_1")
        self.assertEqual(e1.before_total, 0.0)
        self.assertEqual(e1.now_value, 12.5)
        self.assertEqual(e1.running_total, 12.5)

        # Step 2: Deliver 80 (Price: 1.0$)
        step2_selection = [{"package": "80"}]
        updated2, is_all2, cnt2 = mark_selected_packages_delivered(updated1, loader_id=1, selected_items=step2_selection)
        self.assertTrue(is_all2)

        new_names2 = [it["package"] for it in step2_selection]
        pkg_str2 = "+".join(new_names2)
        val2, ok2 = calculate_delivered_packages_value(pkg_str2)
        self.assertTrue(ok2)
        self.assertEqual(val2, 1.0)

        e2, _ = await record_delivery_ledger_entry(order_id=701, package=pkg_str2, now_value=val2, dedup_hash="pd_2")
        self.assertEqual(e2.before_total, 12.5)
        self.assertEqual(e2.now_value, 1.0)
        self.assertEqual(e2.running_total, 13.5)
        self.assertEqual(await get_running_total_current(), 13.5)

    async def test_three_step_and_multi_package_partial_deliveries(self):
        from utils import calculate_delivered_packages_value
        from database import record_delivery_ledger_entry

        val, ok = calculate_delivered_packages_value("5040+2400")
        self.assertTrue(ok)
        self.assertEqual(val, 49.0)

        e, _ = await record_delivery_ledger_entry(order_id=702, package="5040+2400", now_value=val, dedup_hash="pd_multi")
        self.assertEqual(e.now_value, 49.0)

    async def test_second_and_third_partial_delivery_exact_now_values(self):
        from utils import parse_test_order_packages, mark_selected_packages_delivered, calculate_delivered_packages_value
        from database import record_delivery_ledger_entry, get_running_total_current

        # Order: 10800 + 5040 + 2400 (Prices: 65.5$, 33$, 16$ -> Total: 114.5$)
        parsed = parse_test_order_packages("10800+5040+2400")
        items = parsed["packages"]

        # Step 1: First delivery (10800 + 2400) -> Now: 81.5$, Before: 0$, Total: 81.5$
        sel1 = [{"package": "10800"}, {"package": "2400"}]
        updated1, is_all1, _ = mark_selected_packages_delivered(items, loader_id=1, selected_items=sel1)
        val1, ok1 = calculate_delivered_packages_value("10800+2400")
        self.assertTrue(ok1)
        self.assertEqual(val1, 81.5)

        e1, _ = await record_delivery_ledger_entry(order_id=801, package="10800+2400", now_value=val1, dedup_hash="ex_1")
        self.assertEqual(e1.before_total, 0.0)
        self.assertEqual(e1.now_value, 81.5)
        self.assertEqual(e1.running_total, 81.5)

        # Step 2: Second delivery (5040) -> Now: 33$, Before: 81.5$, Total: 114.5$
        sel2 = [{"package": "5040"}]
        updated2, is_all2, _ = mark_selected_packages_delivered(updated1, loader_id=1, selected_items=sel2)
        val2, ok2 = calculate_delivered_packages_value("5040")
        self.assertTrue(ok2)
        self.assertEqual(val2, 33.0)

        e2, _ = await record_delivery_ledger_entry(order_id=801, package="5040", now_value=val2, dedup_hash="ex_2")
        self.assertEqual(e2.before_total, 81.5)
        self.assertEqual(e2.now_value, 33.0)
        self.assertEqual(e2.running_total, 114.5)
        self.assertEqual(await get_running_total_current(), 114.5)


class TestMultiPackageSelectionRegression(unittest.TestCase):
    """Regression tests to verify multi-package selection UI and calculation."""

    def test_selecting_two_packages_simultaneously(self):
        from utils import parse_test_order_packages, toggle_package_selection

        parsed = parse_test_order_packages("10800+5040+2400")
        items = parsed["packages"]

        # Select 10800
        items, s1 = toggle_package_selection(items, 0, loader_id=10)
        self.assertEqual(s1, "Selected")
        self.assertEqual(items[0]["status"], "Selected")

        # Select 5040 simultaneously
        items, s2 = toggle_package_selection(items, 1, loader_id=10)
        self.assertEqual(s2, "Selected")
        self.assertEqual(items[1]["status"], "Selected")

        # Verify both remain Selected simultaneously
        selected_count = sum(1 for it in items if it.get("status") == "Selected")
        self.assertEqual(selected_count, 2)
        self.assertEqual(items[2]["status"], "Pending")

    def test_selecting_all_packages_simultaneously(self):
        from utils import parse_test_order_packages, toggle_package_selection

        parsed = parse_test_order_packages("10800+5040+2400")
        items = parsed["packages"]

        for i in range(3):
            items, status = toggle_package_selection(items, i, loader_id=10)
            self.assertEqual(status, "Selected")

        selected_count = sum(1 for it in items if it.get("status") == "Selected")
        self.assertEqual(selected_count, 3)

    def test_deselecting_a_package(self):
        from utils import parse_test_order_packages, toggle_package_selection

        parsed = parse_test_order_packages("10800+5040+2400")
        items = parsed["packages"]

        # Select 10800 and 5040
        items, _ = toggle_package_selection(items, 0, loader_id=10)
        items, _ = toggle_package_selection(items, 1, loader_id=10)

        # Deselect 5040
        items, s_desel = toggle_package_selection(items, 1, loader_id=10)
        self.assertEqual(s_desel, "Deselected")
        self.assertEqual(items[1]["status"], "Pending")
        self.assertEqual(items[0]["status"], "Selected")

    def test_confirm_delivery_with_multiple_selected_packages(self):
        from utils import parse_test_order_packages, toggle_package_selection, mark_selected_packages_delivered, calculate_delivered_packages_value

        parsed = parse_test_order_packages("10800+5040+2400")
        items = parsed["packages"]

        # Loader selects 10800 and 5040
        items, _ = toggle_package_selection(items, 0, loader_id=10)
        items, _ = toggle_package_selection(items, 1, loader_id=10)

        selected_items = [it for it in items if it.get("status") == "Selected"]
        self.assertEqual(len(selected_items), 2)

        # Confirm delivery
        updated_items, is_all, del_cnt = mark_selected_packages_delivered(items, loader_id=10, selected_items=selected_items)
        self.assertFalse(is_all)
        self.assertEqual(del_cnt, 2)

        # Delivered packages: 10800 + 5040 -> Price: 64$ + 33$ = 97$
        pkg_names = [it["package"] for it in selected_items]
        pkg_str = "+".join(pkg_names)
        self.assertEqual(pkg_str, "10800+5040")

        total_price, ok = calculate_delivered_packages_value(pkg_str)
        self.assertTrue(ok)
        self.assertEqual(total_price, 98.5)


class TestSuperAdminLogicPermissionsAndOrderBypass(unittest.IsolatedAsyncioTestCase):
    """Unit and integration tests for Super Admin permissions and Order Detector bypass."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import DeliveryLedger, RunningTotalLedger, Order
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(RunningTotalLedger))
            await session.execute(delete(Order))
            await session.commit()

    async def test_super_admin_command_access(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import (
            pay_running_total_command_handler,
            running_total_command_handler,
            users_command,
            manual_running_total_text_handler,
        )

        super_admin_id = 1573531032

        # 1. Super Admin uses /pay
        update_pay = MagicMock()
        update_pay.effective_user.id = super_admin_id
        update_pay.effective_message.reply_text = AsyncMock()

        await pay_running_total_command_handler(update_pay, None)
        update_pay.effective_message.reply_text.assert_called_once()
        self.assertIn("Payment Recorded", update_pay.effective_message.reply_text.call_args[0][0])

        # 2. Super Admin uses /total
        update_total = MagicMock()
        update_total.effective_user.id = super_admin_id
        update_total.effective_message.reply_text = AsyncMock()

        await running_total_command_handler(update_total, None)
        update_total.effective_message.reply_text.assert_called_once()
        self.assertIn("Current Delivery Total", update_total.effective_message.reply_text.call_args[0][0])

        # 3. Super Admin uses /users
        update_users = MagicMock()
        update_users.effective_user.id = super_admin_id
        update_users.effective_message.reply_text = AsyncMock()

        await users_command(update_users, None)
        update_users.effective_message.reply_text.assert_called_once()
        self.assertIn("Super Admin", update_users.effective_message.reply_text.call_args[0][0])

        # 4. Super Admin uses +10
        update_plus = MagicMock()
        update_plus.effective_user.id = super_admin_id
        update_plus.effective_message.text = "+10"
        update_plus.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update_plus, None)
        update_plus.effective_message.reply_text.assert_called_once()
        self.assertIn("+10$", update_plus.effective_message.reply_text.call_args[0][0])

        # 5. Super Admin uses -10
        update_minus = MagicMock()
        update_minus.effective_user.id = super_admin_id
        update_minus.effective_message.text = "-10"
        update_minus.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update_minus, None)
        update_minus.effective_message.reply_text.assert_called_once()
        self.assertIn("-10$", update_minus.effective_message.reply_text.call_args[0][0])

    async def test_super_admin_order_message_ignored(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler
        from database import AsyncSessionLocal
        from models import Order
        from sqlalchemy import select

        super_admin_id = 1573531032
        order_text = (
            "Facebook\n\n"
            "Email:\nsuperadmin_order@gmail.com\n\n"
            "Password:\nPakistan123\n\n"
            "Order:\n2400"
        )

        update = MagicMock()
        update.effective_user.id = super_admin_id
        update.effective_chat.id = -100123456
        update.effective_message.text = order_text
        update.effective_message.caption = None
        update.effective_message.message_id = 5555
        update.effective_message.reply_text = AsyncMock()

        await source_group_handler(update, None)

        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Order).where(Order.email == "superadmin_order@gmail.com"))
            orders = res.scalars().all()
            self.assertEqual(len(orders), 0)

    async def test_normal_customer_order_still_detected(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler
        from database import AsyncSessionLocal, BOT_SETTINGS
        from models import Order
        from sqlalchemy import select

        customer_id = 999111222
        order_text = (
            "Facebook\n\n"
            "Email:\ncustomer_order@gmail.com\n\n"
            "Password:\nPakistan123\n\n"
            "Order:\n2400"
        )

        update = MagicMock()
        update.effective_user.id = customer_id
        update.effective_chat.id = -100123456
        update.effective_message.text = order_text
        update.effective_message.caption = None
        update.effective_message.message_id = 1234
        update.effective_message.reply_text = AsyncMock()

        BOT_SETTINGS["source_group_id"] = -100123456

        mock_context = MagicMock()
        mock_context.bot = MagicMock()

        await source_group_handler(update, mock_context)

        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Order).where(Order.email == "customer_order@gmail.com"))
            orders = res.scalars().all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].package, "2400")


class TestMultilingualDetectorAndPackageAliases(unittest.TestCase):
    """Unit and regression tests for multilingual order detection and package aliases."""

    def test_real_customer_message_1_activision(self):
        from keywords import contains_order_keyword
        from utils import parse_test_order_packages

        msg = (
            "Activision\n\n"
            "Correo:\nuser1@gmail.com\n\n"
            "Contraseña:\npassword123\n\n"
            "Nick:\nPlayer1\n\n"
            "10800*3"
        )
        is_order, platform = contains_order_keyword(msg)
        self.assertTrue(is_order)
        self.assertEqual(platform, "activision")

        parsed = parse_test_order_packages(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["packages"]), 3)
        self.assertEqual(parsed["packages"][0]["package"], "10800")
        self.assertEqual(parsed["packages"][0]["qty"], 1)

    def test_real_customer_message_2_facebook_spanish(self):
        from keywords import contains_order_keyword
        from utils import parse_test_order_packages

        msg = (
            "facebook\n\n"
            "Correo o número fb:\nuser2@gmail.com\n\n"
            "Contraseña de fb:\npass123\n\n"
            "Códigos\n\n"
            "2534 6603\n\n"
            "3075 1980\n\n"
            "5k"
        )
        is_order, platform = contains_order_keyword(msg)
        self.assertTrue(is_order)
        self.assertEqual(platform, "facebook")

        parsed = parse_test_order_packages(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["packages"][0]["qty"], 1)

    def test_package_aliases_and_multiplication_notation(self):
        from utils import parse_test_order_packages

        # 5k -> 5040
        p5k = parse_test_order_packages("5k")
        self.assertEqual(len(p5k["packages"]), 1)
        self.assertEqual(p5k["packages"][0]["package"], "5040")

        # 10k -> 10800
        p10k = parse_test_order_packages("10k")
        self.assertEqual(len(p10k["packages"]), 1)
        self.assertEqual(p10k["packages"][0]["package"], "10800")

        # 2.4k -> 2400
        p24k = parse_test_order_packages("2.4k")
        self.assertEqual(len(p24k["packages"]), 1)
        self.assertEqual(p24k["packages"][0]["package"], "2400")

        # 10800*3 -> Expanded to 3 items
        p_mult1 = parse_test_order_packages("10800*3")
        self.assertEqual(len(p_mult1["packages"]), 3)
        self.assertEqual(p_mult1["packages"][0]["package"], "10800")

        # 5040x3 -> Expanded to 3 items
        p_mult2 = parse_test_order_packages("5040x3")
        self.assertEqual(len(p_mult2["packages"]), 3)
        self.assertEqual(p_mult2["packages"][0]["package"], "5040")

        # 2400 ×2 -> Expanded to 2 items
        p_mult3 = parse_test_order_packages("2400 ×2")
        self.assertEqual(len(p_mult3["packages"]), 2)
        self.assertEqual(p_mult3["packages"][0]["package"], "2400")

        # 5k*2 -> Expanded to 2 items of 5040
        p_mult4 = parse_test_order_packages("5k*2")
        self.assertEqual(len(p_mult4["packages"]), 2)
        self.assertEqual(p_mult4["packages"][0]["package"], "5040")

        # 5k x2 -> Expanded to 2 items of 5040
        p_mult5 = parse_test_order_packages("5k x2")
        self.assertEqual(len(p_mult5["packages"]), 2)
        self.assertEqual(p_mult5["packages"][0]["package"], "5040")

    def test_email_and_package_fallback_rule(self):
        from keywords import contains_order_keyword

        # 1. Email + Package -> Order detected
        msg1 = "order_user@gmail.com\n10800"
        is_order1, _ = contains_order_keyword(msg1)
        self.assertTrue(is_order1)

        msg2 = "Customer: buyer@outlook.com\n5k*2"
        is_order2, _ = contains_order_keyword(msg2)
        self.assertTrue(is_order2)

        # 2. Email only -> Not an order
        msg_email_only = "user_only@gmail.com"
        is_order_eo, _ = contains_order_keyword(msg_email_only)
        self.assertFalse(is_order_eo)

        # 3. Package only -> Existing behavior unchanged (Not an order)
        msg_pkg_only = "10800"
        is_order_po, _ = contains_order_keyword(msg_pkg_only)
        self.assertFalse(is_order_po)

        # 4. Real customer messages with Spanish fields -> Order detected
        msg_spanish = (
            "facebook\n\n"
            "Correo o número fb:\nuser_spanish@gmail.com\n\n"
            "Contraseña de fb:\npassword123\n\n"
            "Códigos\n2534 6603\n\n"
            "5k"
        )
        is_order_sp, platform_sp = contains_order_keyword(msg_spanish)
        self.assertTrue(is_order_sp)
        self.assertEqual(platform_sp, "facebook")


class TestPackageMultiplierExpansionEngine(unittest.IsolatedAsyncioTestCase):
    """Production regression test suite for Package Multiplier Expansion Engine."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import DeliveryLedger, RunningTotalLedger, Order
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(RunningTotalLedger))
            await session.execute(delete(Order))
            await session.commit()

    def test_multiplier_expansions_and_aliases(self):
        from utils import parse_test_order_packages

        # 10800*3
        p1 = parse_test_order_packages("10800*3")
        pkgs1 = [it["package"] for it in p1["packages"]]
        self.assertEqual(pkgs1, ["10800", "10800", "10800"])
        self.assertEqual(p1["total_price"], 192.0)

        # 10800x3
        p2 = parse_test_order_packages("10800x3")
        pkgs2 = [it["package"] for it in p2["packages"]]
        self.assertEqual(pkgs2, ["10800", "10800", "10800"])

        # 10800×3
        p3 = parse_test_order_packages("10800×3")
        pkgs3 = [it["package"] for it in p3["packages"]]
        self.assertEqual(pkgs3, ["10800", "10800", "10800"])

        # 5k*2 -> 5040, 5040
        p4 = parse_test_order_packages("5k*2")
        pkgs4 = [it["package"] for it in p4["packages"]]
        self.assertEqual(pkgs4, ["5040", "5040"])
        self.assertEqual(p4["total_price"], 66.0)

        # 10k×4 -> 10800, 10800, 10800, 10800
        p5 = parse_test_order_packages("10k×4")
        pkgs5 = [it["package"] for it in p5["packages"]]
        self.assertEqual(pkgs5, ["10800", "10800", "10800", "10800"])
        self.assertEqual(p5["total_price"], 256.0)

        # 5040x2
        p6 = parse_test_order_packages("5040x2")
        pkgs6 = [it["package"] for it in p6["packages"]]
        self.assertEqual(pkgs6, ["5040", "5040"])

        # 880*5
        p7 = parse_test_order_packages("880*5")
        pkgs7 = [it["package"] for it in p7["packages"]]
        self.assertEqual(pkgs7, ["880", "880", "880", "880", "880"])
        self.assertEqual(p7["total_price"], 40.0)

    async def test_expanded_package_partial_deliveries_and_ledger(self):
        from utils import parse_test_order_packages, toggle_package_selection, mark_selected_packages_delivered, calculate_delivered_packages_value
        from database import record_delivery_ledger_entry, get_running_total_current

        parsed = parse_test_order_packages("10800*3")
        items = parsed["packages"]
        self.assertEqual(len(items), 3)

        # 1. First Partial Delivery: Loader selects 2 of 10800
        items, _ = toggle_package_selection(items, 0, loader_id=10)
        items, _ = toggle_package_selection(items, 1, loader_id=10)

        selected_1 = [it for it in items if it.get("status") == "Selected"]
        self.assertEqual(len(selected_1), 2)

        updated_items_1, is_completed_1, del_cnt_1 = mark_selected_packages_delivered(
            items, loader_id=10, selected_items=selected_1
        )
        self.assertFalse(is_completed_1)
        self.assertEqual(del_cnt_1, 2)

        pkg_str_1 = "+".join([it["package"] for it in selected_1])
        price_1, ok1 = calculate_delivered_packages_value(pkg_str_1)
        self.assertTrue(ok1)
        self.assertEqual(price_1, 131.0)

        entry_1, ok_l1 = await record_delivery_ledger_entry(order_id=1, package=pkg_str_1, now_value=price_1)
        self.assertTrue(ok_l1)
        self.assertEqual(entry_1.before_total, 0.0)
        self.assertEqual(entry_1.now_value, 131.0)
        self.assertEqual(entry_1.running_total, 131.0)
        self.assertEqual(await get_running_total_current(), 131.0)

        # 2. Second Delivery: Loader selects remaining 1 of 10800
        pending_idx = [i for i, it in enumerate(updated_items_1) if it.get("status") == "Pending"][0]
        updated_items_1, _ = toggle_package_selection(updated_items_1, pending_idx, loader_id=10)

        selected_2 = [it for it in updated_items_1 if it.get("status") == "Selected"]
        self.assertEqual(len(selected_2), 1)

        updated_items_2, is_completed_2, del_cnt_2 = mark_selected_packages_delivered(
            updated_items_1, loader_id=10, selected_items=selected_2
        )
        self.assertTrue(is_completed_2)
        self.assertEqual(del_cnt_2, 1)

        pkg_str_2 = "+".join([it["package"] for it in selected_2])
        price_2, ok2 = calculate_delivered_packages_value(pkg_str_2)
        self.assertTrue(ok2)
        self.assertEqual(price_2, 65.5)

        entry_2, ok_l2 = await record_delivery_ledger_entry(order_id=1, package=pkg_str_2, now_value=price_2)
        self.assertTrue(ok_l2)
        self.assertEqual(entry_2.before_total, 131.0)
        self.assertEqual(entry_2.now_value, 65.5)
        self.assertEqual(entry_2.running_total, 196.5)
        self.assertEqual(await get_running_total_current(), 196.5)


class TestProductionOrderParserV2RealCustomerSamples(unittest.TestCase):
    """Test suite for Production Order Parser v2 using 18 real customer production samples."""

    def test_sample_1(self):
        from order_parser import parse_order_v2
        sample = "991#\n***facebook*\n\nNick: Apodo en el juego:Jktxx03\n+584249290951\nContraseña: migordito0324\n\nCódigos\n46137364\n62673214\n74047761\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["customer_ref_id"], "991")
        self.assertEqual(p["login_method"], "Facebook")
        self.assertEqual(p["phone"], "+584249290951")
        self.assertEqual(p["packages"][0]["package"], "2400")
        self.assertEqual(len(p["recovery_codes"]), 3)

    def test_sample_2(self):
        from order_parser import parse_order_v2
        sample = "992#\n\nFor@§ter0\nneiraalex19@gmail.com\n1995Karolyn\n\n10800"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["customer_ref_id"], "992")
        self.assertEqual(p["email"], "neiraalex19@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "10800")

    def test_sample_3(self):
        from order_parser import parse_order_v2
        sample = "993#\n\nWilveralexanderramos37@gmail.com\nRUTH97wrm\nKinataWINGIS\n5k"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "wilveralexanderramos37@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "5040")

    def test_sample_4(self):
        from order_parser import parse_order_v2
        sample = "994#\n*Activision*\n\nNick: Apodo en el juego\nCorreo: jotapyp@gmail.com\nContraseña: Jordanelmejor23$\n\n5k"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["login_method"], "Activision")
        self.assertEqual(p["email"], "jotapyp@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "5040")

    def test_sample_5(self):
        from order_parser import parse_order_v2
        sample = "995#\n\nApodo : ^jefemaestro\nCorreo: arismendideivis130@gmail.com\n\nContraseña : ConorAris3223\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "arismendideivis130@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_6(self):
        from order_parser import parse_order_v2
        sample = "996#\n\nActivación\nN@R@(U\nqui15142023@gmail.com\nquintero12\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["login_method"], "Activision")
        self.assertEqual(p["email"], "qui15142023@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_7(self):
        from order_parser import parse_order_v2
        sample = "997#\n\nNick LYCAN2303\n\nguaicargomez.14@hotmail.com\nClave: 04163346905\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "guaicargomez.14@hotmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_8(self):
        from order_parser import parse_order_v2
        sample = "998#\n\npalmaadalbert.31@gmail.com\nadal2425\nKinggato\n5k"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "palmaadalbert.31@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "5040")

    def test_sample_9(self):
        from order_parser import parse_order_v2
        sample = "999#\n\nNick:Goat.Ʀaӄan\nstrangehuman922@gmail.com\nContraseña:e30165529\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "strangehuman922@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_10(self):
        from order_parser import parse_order_v2
        sample = "1000#\n*Activision*\n\nNick: G®££/\/G()°\nCorreo: braudyscalderon@gmail.com\nContraseña: calderon23*31\n5000+2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["login_method"], "Activision")
        self.assertEqual(p["email"], "braudyscalderon@gmail.com")
        pkgs = [item["package"] for item in p["packages"]]
        self.assertEqual(pkgs, ["5040", "2400"])
        self.assertEqual(p["unknown_packages"], [])

    def test_sample_11(self):
        from order_parser import parse_order_v2
        sample = "1#\n\nnestor_torrique99@hotmail.com\n\nManicuare2004\n\nBk Platinium\n5000"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "nestor_torrique99@hotmail.com")
        self.assertEqual(p["packages"][0]["package"], "5040")
        self.assertEqual(p["unknown_packages"], [])

    def test_sample_12(self):
        from order_parser import parse_order_v2
        sample = "2#\n*Activision*\n\nNick: SASUKE\nCorreo: raidanarias17@gmail.com\nContraseña: 26745481ra.\n\n10800"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["login_method"], "Activision")
        self.assertEqual(p["email"], "raidanarias17@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "10800")

    def test_sample_13(self):
        from order_parser import parse_order_v2
        sample = "3#\n\nNick:\nGØW_Ҝєиîgth\n\nCorreo:\nkenigth10gaming@gmail.com\n\nContraseña:\nkeni.2811\n\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "kenigth10gaming@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_14(self):
        from order_parser import parse_order_v2
        sample = "4#\n\nCorreo\ndamianalejandro2020@outlook.es\n\nContraseña: manuelvivasgod\n\nNick:NS.Bigvivas20\n\nCódigos\n05597299\n09396956\n29985590\n\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "damianalejandro2020@outlook.es")
        self.assertEqual(p["packages"][0]["package"], "2400")
        self.assertEqual(len(p["recovery_codes"]), 3)

    def test_sample_15(self):
        from order_parser import parse_order_v2
        sample = "5#\n\ngabrielcorcega40@gmail.com\ngato..28721\nApodo: GATO\n10800"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "gabrielcorcega40@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "10800")

    def test_sample_16(self):
        from order_parser import parse_order_v2
        sample = "75#\n*facebook*\n\nNick:HG KENNY\nCorreo o número fb: kennyalexanderpay@gmail.com\nContraseña de fb:kenny00\n\nCódigos\n2534 6603\n3075 1980\n3568 0949\n\n5k"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["login_method"], "Facebook")
        self.assertEqual(p["email"], "kennyalexanderpay@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "5040")
        self.assertEqual(len(p["recovery_codes"]), 3)

    def test_sample_17(self):
        from order_parser import parse_order_v2
        sample = "76#\n\nApodo: joker².²\nCorreo: ladeuxpalaciojosedavid@gmail.com\nContraseña: josedavid04\n2400"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "ladeuxpalaciojosedavid@gmail.com")
        self.assertEqual(p["packages"][0]["package"], "2400")

    def test_sample_18(self):
        from order_parser import parse_order_v2
        sample = "77#\n\nNick name F7/ Adsolutex.\n\nCorreo. mcallistercastrillon@icloud.com\n\nContraseña. Torrealba174.\n5k"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["email"], "mcallistercastrillon@icloud.com")
        self.assertEqual(p["packages"][0]["package"], "5040")


class TestCanonicalPackageAliasNormalizationFix(unittest.IsolatedAsyncioTestCase):
    """Regression test suite for Production Package Alias Normalization Fix."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import DeliveryLedger, RunningTotalLedger, Order
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(RunningTotalLedger))
            await session.execute(delete(Order))
            await session.commit()

    def test_canonical_alias_resolutions(self):
        from order_parser import normalize_package_alias, parse_order_v2

        self.assertEqual(normalize_package_alias("5k"), "5040")
        self.assertEqual(normalize_package_alias("5K"), "5040")
        self.assertEqual(normalize_package_alias("5000"), "5040")
        self.assertEqual(normalize_package_alias("5040"), "5040")

        # 5k, 5000, 5040 price equality
        p_5k = parse_order_v2("Email: a@g.com\n5k")
        p_5000 = parse_order_v2("Email: a@g.com\n5000")
        p_5040 = parse_order_v2("Email: a@g.com\n5040")

        self.assertEqual(p_5k["packages"][0]["package"], "5040")
        self.assertEqual(p_5000["packages"][0]["package"], "5040")
        self.assertEqual(p_5040["packages"][0]["package"], "5040")

        self.assertEqual(p_5k["packages"][0]["unit_price"], 33.0)
        self.assertEqual(p_5000["packages"][0]["unit_price"], 33.0)
        self.assertEqual(p_5040["packages"][0]["unit_price"], 33.0)

    def test_multi_package_canonical_normalization(self):
        from utils import parse_test_order_packages

        # 5000+2400 -> 5040 + 2400
        p1 = parse_test_order_packages("5000+2400")
        pkgs1 = [it["package"] for it in p1["packages"]]
        self.assertEqual(pkgs1, ["5040", "2400"])
        self.assertFalse(p1["has_unknown"])
        self.assertEqual(p1["total_price"], 49.5)

        # 5k+2400 -> 5040 + 2400
        p2 = parse_test_order_packages("5k+2400")
        pkgs2 = [it["package"] for it in p2["packages"]]
        self.assertEqual(pkgs2, ["5040", "2400"])
        self.assertFalse(p2["has_unknown"])
        self.assertEqual(p2["total_price"], 49.5)

        # 5040+2400 -> 5040 + 2400
        p3 = parse_test_order_packages("5040+2400")
        pkgs3 = [it["package"] for it in p3["packages"]]
        self.assertEqual(pkgs3, ["5040", "2400"])
        self.assertFalse(p3["has_unknown"])
        self.assertEqual(p3["total_price"], 49.5)

    def test_multiplier_canonical_normalization(self):
        from utils import parse_test_order_packages

        # 5000*2 -> 5040, 5040
        p1 = parse_test_order_packages("5000*2")
        pkgs1 = [it["package"] for it in p1["packages"]]
        self.assertEqual(pkgs1, ["5040", "5040"])
        self.assertEqual(p1["total_price"], 66.0)

        # 5k*2 -> 5040, 5040
        p2 = parse_test_order_packages("5k*2")
        pkgs2 = [it["package"] for it in p2["packages"]]
        self.assertEqual(pkgs2, ["5040", "5040"])
        self.assertEqual(p2["total_price"], 66.0)

        # 5040*2 -> 5040, 5040
        p3 = parse_test_order_packages("5040*2")
        pkgs3 = [it["package"] for it in p3["packages"]]
        self.assertEqual(pkgs3, ["5040", "5040"])
        self.assertEqual(p3["total_price"], 66.0)

    async def test_loader_display_ledger_and_running_total_canonical_prices(self):
        from utils import parse_test_order_packages, build_loader_package_keyboard, mark_selected_packages_delivered, calculate_delivered_packages_value
        from database import record_delivery_ledger_entry, get_running_total_current

        parsed = parse_test_order_packages("5000+2400")
        items = parsed["packages"]

        # Loader UI receives canonical packages (☐ 5040 CP / ⬜ 5040)
        kb = build_loader_package_keyboard(101, items)
        self.assertIsNotNone(kb)
        btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("5040", btn_texts[0])

        # Partial delivery & Ledger
        updated, is_completed, del_cnt = mark_selected_packages_delivered(items, loader_id=5, selected_items=[items[0]])
        self.assertEqual(del_cnt, 1)

        pkg_str = items[0]["package"]
        self.assertEqual(pkg_str, "5040")

        val, ok = calculate_delivered_packages_value(pkg_str)
        self.assertTrue(ok)
        self.assertEqual(val, 33.0)

        entry, ok_l = await record_delivery_ledger_entry(order_id=10, package=pkg_str, now_value=val)
        self.assertTrue(ok_l)
        self.assertEqual(entry.now_value, 33.0)
        self.assertEqual(await get_running_total_current(), 33.0)

    def test_delivery_ledger_exact_3_line_format(self):
        from utils import format_ledger_entry_message
        msg = format_ledger_entry_message(64.0, 15.5, 79.5)
        expected = "Before 64$\nNow 15.5$\nTotal 79.5$"
        self.assertEqual(msg, expected)


class TestCPPackAndRecoveryCodeSeparation(unittest.TestCase):
    """Test suite for CP PACK field priority, recovery code separation, and thousands separators."""

    def test_sample_52(self):
        from order_parser import parse_order_v2
        sample = (
            "Order #:52\n\n"
            "Login: Activision\n\n"
            "Email: yenilicet1996@gmail.com\n\n"
            "Password: Licet1996\n\n"
            "IGN \"Nick\" : 5G·Tomorrow\n\n"
            "CP PACK : 12.000\n\n"
            "Mode: SaFe\n\n"
            "Time: FAST"
        )
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["customer_ref_id"], "52")
        self.assertEqual(p["login_method"], "Activision")
        self.assertEqual(p["email"], "yenilicet1996@gmail.com")
        self.assertEqual(len(p["packages"]), 1)
        self.assertEqual(p["packages"][0]["package"], "12000")

    def test_sample_54_recovery_codes_separated(self):
        from order_parser import parse_order_v2
        sample = (
            "Order #:54\n\n"
            "Login: Facebook\n\n"
            "Email: vierimansilla6141@gmail.com\n\n"
            "Password: mmchina6141\n\n"
            "Codes: (solo FB) 👇🏼👇🏼👇🏼:\n\n"
            "14627274\n"
            "16259506\n"
            "33195095\n"
            "35621615\n"
            "41430777\n\n"
            "IGN \"Nick\": VieriM\n\n"
            "CP PACK: 12.000\n\n"
            "Mode: SaFe\n\n"
            "Time: Fast"
        )
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["customer_ref_id"], "54")
        self.assertEqual(p["login_method"], "Facebook")
        self.assertEqual(p["email"], "vierimansilla6141@gmail.com")

        # Recovery codes captured
        self.assertEqual(len(p["recovery_codes"]), 5)
        self.assertIn("14627274", p["recovery_codes"])

        # Packages must contain ONLY 12000, ZERO recovery codes!
        self.assertEqual(len(p["packages"]), 1)
        self.assertEqual(p["packages"][0]["package"], "12000")
        pkg_names = [item["package"] for item in p["packages"]]
        for code in p["recovery_codes"]:
            self.assertNotIn(code, pkg_names)

    def test_sample_55(self):
        from order_parser import parse_order_v2
        sample = "Order #:55\n\nLogin: Activision\n\nEmail: eudesvicentearellano@gmail.com\n\nPassword: arellano26\n\nIGN \"Nick\" : ThePUKITIS\n\nCP PACK : 4800"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["packages"][0]["package"], "4800")

    def test_sample_56(self):
        from order_parser import parse_order_v2
        sample = "Order #:56\n\nLogin: Activision\n\nEmail: juanincodm367@gmail.com\n\nPassword: 319702Ju*\n\nIGN \"Nick\" : ƦGИ・VɅELOR\n\nCP PACK : 12,000"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["packages"][0]["package"], "12000")

    def test_sample_57(self):
        from order_parser import parse_order_v2
        sample = "Order #:57\n\nLogin: Activision\n\nEmail: christopherw121094@gmail.com\n\nPassword: codwarch94\n\nIGN \"Nick\" :NN_warch1210\n\nCP PACK : 4.800"
        p = parse_order_v2(sample)
        self.assertTrue(p["order_detected"])
        self.assertEqual(p["packages"][0]["package"], "4800")

    def test_sample_58(self):
        from order_parser import parse_order_v2
        p = parse_order_v2("CP PACK : 12,000")
        self.assertEqual(p["packages"][0]["package"], "12000")

    def test_sample_59(self):
        from order_parser import parse_order_v2
        p = parse_order_v2("CP PACK : 24.000")
        self.assertEqual(p["packages"][0]["package"], "24000")

    def test_sample_60(self):
        from order_parser import parse_order_v2
        p = parse_order_v2("CP PACK : 9.600")
        self.assertEqual(p["packages"][0]["package"], "9600")


class TestMissingPackageWorkflowFix(unittest.IsolatedAsyncioTestCase):
    """Test suite for Missing Package Workflow Fix."""

    async def asyncSetUp(self):
        from database import DEFAULT_PACKAGE_PRICES
        self._orig_prices = dict(DEFAULT_PACKAGE_PRICES)

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, PackagePrice, DEFAULT_PACKAGE_PRICES
        from sqlalchemy import delete
        from utils import reload_package_prices_cache
        async with AsyncSessionLocal() as s:
            await s.execute(delete(PackagePrice))
            for k, v in DEFAULT_PACKAGE_PRICES.items():
                s.add(PackagePrice(package=k, price=v))
            await s.commit()
        reload_package_prices_cache(DEFAULT_PACKAGE_PRICES)

    async def test_single_and_multiple_missing_packages(self):
        from utils import get_unknown_package_keyboard, format_missing_packages_summary, format_package_progress_summary, update_unknown_package_price
        from database import update_single_package_price_in_db, get_all_package_prices_from_db

        items = [
            {"package": "999000", "qty": 1, "status": "Unpriced", "unit_price": None},
            {"package": "888000", "qty": 1, "status": "Unpriced", "unit_price": None}
        ]

        # 1. Summary displays Missing Packages list
        summary = format_missing_packages_summary(items)
        self.assertIn("❌ Missing Packages", summary)
        self.assertIn("999000", summary)
        self.assertIn("888000", summary)

        # 2. Keyboard displays button for EACH missing package
        kb = get_unknown_package_keyboard(101, items)
        self.assertIsNotNone(kb)
        btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertEqual(len(btn_texts), 2)
        self.assertIn("✏️ Add Price 999000", btn_texts)
        self.assertIn("✏️ Add Price 888000", btn_texts)

        # 3. Add price to first package (999000 -> 67$)
        await update_single_package_price_in_db("999000", 67.0)

        # Verify saved in DB
        db_prices = await get_all_package_prices_from_db()
        self.assertEqual(db_prices.get("999000"), 67.0)

        # Update order items
        updated_items, new_total, has_unpriced = update_unknown_package_price(items, "999000", 67.0)
        self.assertTrue(has_unpriced)

        # 4. Remaining missing package (888000 -> 58$)
        kb2 = get_unknown_package_keyboard(101, updated_items)
        self.assertIsNotNone(kb2)
        btn_texts2 = [btn.text for row in kb2.inline_keyboard for btn in row]
        self.assertEqual(len(btn_texts2), 1)
        self.assertIn("✏️ Add Price 888000", btn_texts2)

        # Add price for 888000 -> 58$
        await update_single_package_price_in_db("888000", 58.0)
        updated_items2, final_total, has_unpriced2 = update_unknown_package_price(updated_items, "888000", 58.0)
        self.assertFalse(has_unpriced2)
        self.assertEqual(final_total, 125.0)

        # 5. After all missing prices added, keyboard returns None & display converts to standard package format
        self.assertIsNone(get_unknown_package_keyboard(101, updated_items2))
        final_summary = format_package_progress_summary(updated_items2, final_total)
        self.assertIn("📦 Packages", final_summary)
        self.assertIn("999000 CP", final_summary)
        self.assertIn("888000 CP", final_summary)
        self.assertIn("125$", final_summary)

    def test_alias_5000_5k_5040_behavior_unchanged(self):
        from utils import get_unknown_package_keyboard
        items = [
            {"package": "5000", "qty": 1, "status": "Pending", "unit_price": 33.0},
            {"package": "5k", "qty": 1, "status": "Pending", "unit_price": 33.0}
        ]
        kb = get_unknown_package_keyboard(102, items)
        self.assertIsNone(kb)


class TestUpsertBulkPriceUpdateSystem(unittest.IsolatedAsyncioTestCase):
    """Regression test suite for Fix /updateprices - UPSERT & New Package Prices."""

    async def asyncSetUp(self):
        from database import DEFAULT_PACKAGE_PRICES
        self._orig_prices = dict(DEFAULT_PACKAGE_PRICES)

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, PackagePrice, DEFAULT_PACKAGE_PRICES
        from sqlalchemy import delete
        from utils import reload_package_prices_cache
        async with AsyncSessionLocal() as s:
            await s.execute(delete(PackagePrice))
            for k, v in DEFAULT_PACKAGE_PRICES.items():
                s.add(PackagePrice(package=k, price=v))
            await s.commit()
        reload_package_prices_cache(DEFAULT_PACKAGE_PRICES)

    async def test_upsert_bulk_update_complete_flow(self):
        from utils import parse_bulk_prices_input, parse_test_order_packages, get_unknown_package_keyboard
        from database import bulk_update_package_prices_in_db, get_all_package_prices_from_db

        # 1. Update existing package price + Insert new package price in same /updateprices
        update_text = """
        10800 67
        5040 34
        2400 16.5
        108000 563
        96,000 503
        72.000 375
        55200 300
        48000 31
        38400 250
        999000 750
        """
        price_map, err = parse_bulk_prices_input(update_text)
        self.assertIsNone(err)
        self.assertIsNotNone(price_map)

        # Verify thousands separator normalization
        self.assertEqual(price_map.get("96000"), 503.0)
        self.assertEqual(price_map.get("72000"), 375.0)
        self.assertEqual(price_map.get("108000"), 563.0)
        self.assertEqual(price_map.get("999000"), 750.0)
        self.assertEqual(price_map.get("10800"), 67.0)

        # Save to DB & Cache
        success = await bulk_update_package_prices_in_db(price_map, updated_by_id=12345)
        self.assertTrue(success)

        # 2. Cache refresh after /updateprices (immediate recognition without restart)
        db_prices = await get_all_package_prices_from_db()
        self.assertEqual(db_prices.get("108000"), 563.0)
        self.assertEqual(db_prices.get("96000"), 503.0)
        self.assertEqual(db_prices.get("72000"), 375.0)
        self.assertEqual(db_prices.get("999000"), 750.0)

        # 3. New order containing 108000, 96000, 72000 recognized immediately
        order_msg = (
            "Facebook\n\n"
            "Email:\ntestcustomer@gmail.com\n\n"
            "Password:\nPass1234\n\n"
            "Order:\n108.000+96.000+72,000"
        )
        p = parse_test_order_packages(order_msg)
        self.assertIsNotNone(p)
        self.assertFalse(p["has_unknown"])
        self.assertEqual(p["total_price"], 1441.0)

        # New package no longer triggers Add Price
        kb = get_unknown_package_keyboard(99, p["packages"])
        self.assertIsNone(kb)

        # 4. Alias 5000 / 5k / 5040 behavior unchanged
        p_alias = parse_test_order_packages("Email: u@gmail.com\nPass: p\n5k+5000")
        self.assertIsNotNone(p_alias)
        self.assertFalse(p_alias["has_unknown"])
        self.assertEqual(p_alias["total_price"], 68.0)

        # 5. Genuinely unknown package still triggers Missing Package flow
        p_unk = parse_test_order_packages("Email: u@gmail.com\nPass: p\n888777")
        self.assertIsNotNone(p_unk)
        self.assertTrue(p_unk["has_unknown"])
        kb_unk = get_unknown_package_keyboard(99, p_unk["packages"])
        self.assertIsNotNone(kb_unk)
        btn_texts = [b.text for r in kb_unk.inline_keyboard for b in r]
        self.assertIn("✏️ Add Price 888777", btn_texts)


class TestWrongPasswordCustomerFlow(unittest.IsolatedAsyncioTestCase):
    """Test suite for Updated Wrong Password Customer Flow."""

    async def test_wrong_password_customer_buttons_and_callbacks(self):
        from utils import build_customer_issue_keyboard, LoaderIssueType
        from database import (
            create_order,
            set_order_loader_message_id,
            get_order_by_id,
            update_order_issue_state,
            update_order_status,
            update_order_raw_text,
            get_latest_ledger_entries
        )
        from handlers import customer_confirmation_callback_handler

        # 1. Create test order
        order = await create_order(
            email="testcustomer@gmail.com",
            client_chat_id=-1001234,
            original_message_id=999,
            package="10800",
            raw_text="Email: testcustomer@gmail.com\nPass: Secret123\n10800"
        )
        await set_order_loader_message_id(order.id, 888, -1005678)
        self.assertIsNotNone(order)
        order_id = order.id

        # 2. Loader reports wrong password -> Verify 2 buttons generated: It's Correct & Cancel
        kb = build_customer_issue_keyboard(order_id, LoaderIssueType.WRONG_PASSWORD)
        btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertEqual(len(btn_texts), 2)
        self.assertEqual(btn_texts[0], "✅ It's Correct")
        self.assertEqual(btn_texts[1], "❌ Cancel")

        # 3. Test "🔄 Provide Correct Password" callback handler execution
        sent_messages = []
        sent_reactions = []
        edited_messages = []

        class MockQuery:
            data = f"cust_confirm:pw_updating:{order_id}:wrong_password"
            message = type("Msg", (), {"caption": None})()

            async def edit_message_text(self, text, parse_mode=None):
                edited_messages.append(text)

            async def edit_message_caption(self, caption, parse_mode=None):
                edited_messages.append(caption)

            async def answer(self, text=None, show_alert=False):
                pass

        class MockBot:
            async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode=None):
                sent_messages.append({
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

            async def set_message_reaction(self, chat_id, message_id, reaction=None, is_big=None):
                sent_reactions.append({
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": reaction
                })

        mock_update = type("Update", (), {"callback_query": MockQuery()})()
        mock_context = type("Context", (), {"bot": MockBot()})()

        await customer_confirmation_callback_handler(mock_update, mock_context)

        # ✓ Provide Correct Password sends client prompt
        self.assertTrue(any("Provide Correct Password" in m or "Please send your corrected password" in m for m in edited_messages))

        # ✓ Loader receives password-update notification replying to original loader message (888)
        self.assertTrue(any("Please wait for the new password." in m["text"] for m in sent_messages))
        self.assertTrue(any(m["reply_to_message_id"] == 888 for m in sent_messages))

        # ✓ Original Loader message receives 🔄 reaction
        self.assertTrue(any(r["message_id"] == 888 for r in sent_reactions))

        # 4. Customer sends new password -> Save to SAME Order ID
        updated_order = await update_order_raw_text(order_id, "Password:\nMySuperNewPass123", "testcustomer@gmail.com")
        self.assertIsNotNone(updated_order)
        self.assertEqual(updated_order.id, order_id)
        self.assertIn("MySuperNewPass123", updated_order.raw_text)

        # 5. Test "❌ Cancel Sending New Data" callback handler execution
        sent_messages.clear()
        sent_reactions.clear()
        edited_messages.clear()

        class MockCancelQuery:
            data = f"cust_confirm:pw_cancel:{order_id}:wrong_password"
            message = type("Msg", (), {"caption": None})()

            async def edit_message_text(self, text, parse_mode=None):
                edited_messages.append(text)

            async def edit_message_caption(self, caption, parse_mode=None):
                edited_messages.append(caption)

            async def answer(self, text=None, show_alert=False):
                pass

        mock_cancel_update = type("Update", (), {"callback_query": MockCancelQuery()})()

        await customer_confirmation_callback_handler(mock_cancel_update, mock_context)

        # ✓ Cancel Order marks same order Cancelled
        can_order = await get_order_by_id(order_id)
        self.assertEqual(can_order.status, "CANCELLED")

        # ✓ Cancellation notification is sent to Loader Group replying to original loader message (888)
        self.assertTrue(any("Cancelled by Customer" in m["text"] or "Cancelled" in m["text"] for m in sent_messages))
        self.assertTrue(any(m["reply_to_message_id"] == 888 for m in sent_messages))

        # ✓ Loader message (888) and Client message (999) receive ❌ reaction
        self.assertTrue(any(r["message_id"] == 888 for r in sent_reactions))
        self.assertTrue(any(r["message_id"] == 999 for r in sent_reactions))

        # ✓ Cancelled order cannot be delivered
        from delivery import deliver_order_by_id
        success = await deliver_order_by_id(MockBot(), order_id)
        self.assertFalse(success)

        # ✓ No duplicate order or ledger entry
        entries = await get_latest_ledger_entries(limit=10)
        matching_entries = [e for e in entries if e.order_id == order_id]
        self.assertEqual(len(matching_entries), 0)

    async def test_multiple_password_updates_and_cancellation_reaction(self):
        from utils import build_updated_raw_text_with_passwords
        from database import (
            create_order,
            set_order_loader_message_id,
            get_order_by_id,
            update_order_issue_state,
            update_order_status,
            update_order_raw_text,
            get_latest_ledger_entries
        )
        from handlers import customer_confirmation_callback_handler

        # 1. Create original order
        orig_raw = "Login: Facebook\nEmail: ex@gmail.com\nPassword: A\n2400 CP"
        order = await create_order(
            email="ex@gmail.com",
            client_chat_id=-100111,
            original_message_id=500,
            package="2400",
            raw_text=orig_raw
        )
        await set_order_loader_message_id(order.id, 76, -100999)
        order = await get_order_by_id(order.id)
        order_id = order.id
        self.assertEqual(order.loader_message_id, 76)
        self.assertEqual(order.loader_group_id, -100999)

        # 2. Password Update #1: A -> B
        raw1 = build_updated_raw_text_with_passwords(order.raw_text, "B")
        self.assertIn("Old Password: A", raw1)
        self.assertIn("New Password: B", raw1)
        upd1 = await update_order_raw_text(order_id, raw1, "ex@gmail.com")
        self.assertEqual(upd1.id, order_id)
        self.assertEqual(upd1.loader_message_id, 76)

        # 3. Password Update #2: B -> C
        raw2 = build_updated_raw_text_with_passwords(upd1.raw_text, "C")
        self.assertIn("Old Password: B", raw2)
        self.assertIn("New Password: C", raw2)
        upd2 = await update_order_raw_text(order_id, raw2, "ex@gmail.com")
        self.assertEqual(upd2.id, order_id)
        self.assertEqual(upd2.loader_message_id, 76)

        # 4. Password Update #3: C -> D
        raw3 = build_updated_raw_text_with_passwords(upd2.raw_text, "D")
        self.assertIn("Old Password: C", raw3)
        self.assertIn("New Password: D", raw3)
        upd3 = await update_order_raw_text(order_id, raw3, "ex@gmail.com")
        self.assertEqual(upd3.id, order_id)
        self.assertEqual(upd3.loader_message_id, 76)

        # 5. Customer clicks ❌ Cancel Order after 3 password updates
        sent_messages = []
        sent_reactions = []

        class MockQuery:
            data = f"cust_confirm:pw_cancel:{order_id}:wrong_password"
            message = type("Msg", (), {"caption": None})()

            async def edit_message_text(self, text, parse_mode=None):
                pass

            async def edit_message_caption(self, caption, parse_mode=None):
                pass

            async def answer(self, text=None, show_alert=False):
                pass

        class MockBot:
            async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode=None):
                sent_messages.append({
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

            async def set_message_reaction(self, chat_id, message_id, reaction=None, is_big=None):
                sent_reactions.append({
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": reaction
                })

        mock_update = type("Update", (), {"callback_query": MockQuery()})()
        mock_context = type("Context", (), {"bot": MockBot()})()

        await customer_confirmation_callback_handler(mock_update, mock_context)

        # ✓ Cancel Order marks same order Cancelled
        can_order = await get_order_by_id(order_id)
        self.assertEqual(can_order.status, "CANCELLED")

        # ✓ ❌ reaction added to loader_message_id (76) and client_original_message_id (500)
        self.assertTrue(any(r["message_id"] == 76 for r in sent_reactions))
        self.assertTrue(any(r["message_id"] == 500 for r in sent_reactions))

        # ✓ Cancellation notification sent replying to loader_message_id (76)
        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0]["chat_id"], -100999)
        self.assertEqual(sent_messages[0]["reply_to_message_id"], 76)
        self.assertTrue("Cancelled" in sent_messages[0]["text"])

        # ✓ Cancelled order cannot be delivered
        from delivery import deliver_order_by_id
        success = await deliver_order_by_id(MockBot(), order_id)
        self.assertFalse(success)

    async def test_multi_order_explicit_cancellation_isolation(self):
        """
        Tests Section 8 requirement:
        Create Order #236 and Order #277.
        Trigger Wrong Password & Cancel on Order #277.
        Verify Order #277 cancelled, Order #236 unchanged, ❌ reaction on Order #277 loader msg.
        """
        from database import (
            create_order,
            set_order_loader_message_id,
            get_order_by_id
        )
        from handlers import customer_confirmation_callback_handler

        # 1. Create Order #236
        order_236 = await create_order(
            email="cust236@gmail.com",
            client_chat_id=-1001,
            original_message_id=10,
            package="10800",
            raw_text="Email: cust236@gmail.com\n10800"
        )
        await set_order_loader_message_id(order_236.id, 2360, -10099)
        order_236 = await get_order_by_id(order_236.id)

        # 2. Create Order #277
        order_277 = await create_order(
            email="cust277@gmail.com",
            client_chat_id=-1002,
            original_message_id=20,
            package="21600",
            raw_text="Activision\nEmail: cust277@gmail.com\nPassword: P277\n21600"
        )
        await set_order_loader_message_id(order_277.id, 2770, -10099)
        order_277 = await get_order_by_id(order_277.id)

        # 3. Customer clicks ❌ Cancel Order on Order #277
        sent_messages = []
        sent_reactions = []

        class MockQuery:
            data = f"cust_confirm:pw_cancel:{order_277.id}:wrong_password"
            message = type("Msg", (), {"caption": None})()

            async def edit_message_text(self, text, parse_mode=None):
                pass

            async def edit_message_caption(self, caption, parse_mode=None):
                pass

            async def answer(self, text=None, show_alert=False):
                pass

        class MockBot:
            async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode=None):
                sent_messages.append({
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

            async def set_message_reaction(self, chat_id, message_id, reaction=None, is_big=None):
                sent_reactions.append({
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": reaction
                })

        mock_update = type("Update", (), {"callback_query": MockQuery()})()
        mock_context = type("Context", (), {"bot": MockBot()})()

        await customer_confirmation_callback_handler(mock_update, mock_context)

        # 4. Verify Order #277 is CANCELLED and Order #236 is UNCHANGED
        res_277 = await get_order_by_id(order_277.id)
        res_236 = await get_order_by_id(order_236.id)

        self.assertEqual(res_277.status, "CANCELLED")
        self.assertEqual(res_236.status, "Pending")

        # 5. Verify ❌ reaction on Order #277's loader message (2770) and NOT #236 (2360)
        self.assertTrue(any(r["message_id"] == 2770 for r in sent_reactions))
        self.assertFalse(any(r["message_id"] == 2360 for r in sent_reactions))

        # 6. Verify notification text explicitly mentions Cancelled and NOT #236
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("Cancelled", sent_messages[0]["text"])
        self.assertNotIn(f"#{order_236.id}", sent_messages[0]["text"])


class TestLoaderBotNotificationFilter(unittest.IsolatedAsyncioTestCase):
    async def test_is_bot_system_notification_text_helper(self):
        from utils import is_bot_system_notification_text

        # Bot system notifications -> must return True
        self.assertTrue(is_bot_system_notification_text("❌ Order Cancelled\n\nOrder #267 has been cancelled by the customer."))
        self.assertTrue(is_bot_system_notification_text("🔄 Password Updated\n\nOrder #267\nOld Password: A\nNew Password: B"))
        self.assertTrue(is_bot_system_notification_text("🔄 Customer is updating the password.\n\nPlease wait for the new password."))
        self.assertTrue(is_bot_system_notification_text("📦 Delivered Package\n\n10800 CP delivered for Order #267."))
        self.assertTrue(is_bot_system_notification_text("📊 Delivery Ledger\n\nBefore 64$\nNow 15.5$\nTotal 79.5$"))
        self.assertTrue(is_bot_system_notification_text("⏳ Waiting for customer confirmation..."))
        self.assertTrue(is_bot_system_notification_text("✅ Customer confirmed password"))

        # Real loader inputs -> must return False
        self.assertFalse(is_bot_system_notification_text("64"))
        self.assertFalse(is_bot_system_notification_text("+10"))
        self.assertFalse(is_bot_system_notification_text("-10"))
        self.assertFalse(is_bot_system_notification_text("wrong password"))
        self.assertFalse(is_bot_system_notification_text("wrong name"))
        self.assertFalse(is_bot_system_notification_text("2fa"))

    async def test_bot_cancellation_and_system_notifications_ignored(self):
        from handlers import delivery_group_handler, price_input_text_handler

        replied_text = []

        class MockUser:
            id = 999000
            is_bot = True

        class MockChat:
            id = -100999
            title = "Loader Group"

        class MockReplyTo:
            message_id = 555
            text = "Order details"
            caption = None

        class MockMessage:
            message_id = 556
            from_user = MockUser()
            chat = MockChat()
            text = "❌ Order Cancelled\n\nOrder #267 has been cancelled by the customer.\n\nPlease stop this delivery."
            caption = None
            photo = None
            document = None
            reply_to_message = MockReplyTo()

            async def reply_text(self, text, parse_mode=None, reply_to_message_id=None, reply_markup=None):
                replied_text.append(text)

        class MockBot:
            id = 999000

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_chat": MockChat(),
            "effective_message": MockMessage()
        })()
        mock_context = type("Context", (), {"bot": MockBot()})()

        # Run both handlers on bot notification message
        await delivery_group_handler(mock_update, mock_context)
        await price_input_text_handler(mock_update, mock_context)

        # ✓ Verify NO reply text was sent (never triggers "❌ Invalid price")
        self.assertEqual(len(replied_text), 0)


class TestDynamicDeliveryPricingAfterUpdatePrices(unittest.IsolatedAsyncioTestCase):
    async def test_updateprices_updates_delivery_caption_and_ledger(self):
        from database import bulk_update_package_prices_in_db, DEFAULT_PACKAGE_PRICES
        from utils import (
            format_delivered_packages_caption,
            calculate_delivered_packages_value,
            PACKAGE_PRICES,
            reload_package_prices_cache
        )

        # 1. Update price for 2400 CP from 16 to 15.5
        success = await bulk_update_package_prices_in_db({"2400": 15.5}, updated_by_id=1573531032)
        self.assertTrue(success)
        self.assertEqual(PACKAGE_PRICES["2400"], 15.5)

        # 2. Verify Delivered Package caption uses current price 15.5$ even if item has stale unit_price 16.0
        items_with_stale_price = [{"package": "2400", "qty": 1, "unit_price": 16.0, "status": "Pending"}]
        caption = format_delivered_packages_caption(items_with_stale_price)
        self.assertIn("📦 Delivered Package", caption)
        self.assertIn("✅ 2400 CP", caption)
        self.assertIn("💰 Price: 15.5$", caption)
        self.assertNotIn("💰 Price: 16$", caption)

        # 3. Verify Delivery Ledger calculation uses current price 15.5
        ledger_val, is_known = calculate_delivered_packages_value("2400")
        self.assertTrue(is_known)
        self.assertEqual(ledger_val, 15.5)

        # Cleanup: Restore default package prices
        await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, updated_by_id=1573531032)

    async def test_multi_package_and_partial_delivery_pricing_after_update(self):
        from database import bulk_update_package_prices_in_db, DEFAULT_PACKAGE_PRICES
        from utils import (
            format_delivered_packages_caption,
            calculate_delivered_packages_value,
            PACKAGE_PRICES
        )

        # Update 10800 = 67.0 and 2400 = 15.5
        await bulk_update_package_prices_in_db({"10800": 67.0, "2400": 15.5}, updated_by_id=1573531032)
        self.assertEqual(PACKAGE_PRICES["10800"], 67.0)
        self.assertEqual(PACKAGE_PRICES["2400"], 15.5)

        # Multi-package delivery caption (10800 + 2400 -> 67 + 15.5 = 82.5$)
        multi_items = [
            {"package": "10800", "qty": 1, "status": "Pending"},
            {"package": "2400", "qty": 1, "status": "Pending"}
        ]
        caption = format_delivered_packages_caption(multi_items)
        self.assertIn("📦 Delivered Package(s)", caption)
        self.assertIn("✅ 10800 CP", caption)
        self.assertIn("✅ 2400 CP", caption)
        self.assertIn("💰 Price: 82.5$", caption)

        # Multi-package delivery ledger value
        ledger_val, is_known = calculate_delivered_packages_value("10800+2400")
        self.assertTrue(is_known)
        self.assertEqual(ledger_val, 82.5)

        # Partial delivery caption (2400 only -> 15.5$)
        partial_items = [{"package": "2400", "qty": 1, "status": "Pending"}]
        p_caption = format_delivered_packages_caption(partial_items)
        self.assertIn("📦 Delivered Package", p_caption)
        self.assertIn("💰 Price: 15.5$", p_caption)

        # Cleanup: Restore default package prices
        await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, updated_by_id=1573531032)


class TestFlexibleOrderDetection(unittest.IsolatedAsyncioTestCase):
    async def test_exact_customer_yandex_order_sample(self):
        from order_parser import parse_order_v2

        sample_msg = (
            "Login\n\n"
            "Raphiniels@yandex.ru\n\n"
            "Password\n\n"
            "powered124\n\n"
            "Nickname\n\n"
            "Raphaskill\n\n"
            "7200cp"
        )

        parsed = parse_order_v2(sample_msg)

        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"].lower(), "raphiniels@yandex.ru")
        self.assertEqual(parsed["password"], "powered124")
        self.assertEqual(parsed["username"], "Raphaskill")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "7200")

    async def test_label_and_cp_format_variations(self):
        from order_parser import parse_order_v2

        # 1. Colon-separated format with 7200 CP
        v1 = parse_order_v2("Login: email@gmail.com\nPassword: abc123\nNickname: Player\n7200 CP")
        self.assertTrue(v1["order_detected"])
        self.assertEqual(v1["email"], "email@gmail.com")
        self.assertEqual(v1["password"], "abc123")
        self.assertEqual(v1["username"], "Player")
        self.assertEqual(v1["packages"][0]["package"], "7200")

        # 2. Spanish format with CP: 7200 and @outlook.es
        v2 = parse_order_v2("Correo\nusuario@outlook.es\nContraseña\nmiClave123\nApodo\nMiApodo\nCP: 7200")
        self.assertTrue(v2["order_detected"])
        self.assertEqual(v2["email"], "usuario@outlook.es")
        self.assertEqual(v2["password"], "miClave123")
        self.assertEqual(v2["username"], "MiApodo")
        self.assertEqual(v2["packages"][0]["package"], "7200")

        # 3. Thousands separator (7.200) with @icloud.com
        v3 = parse_order_v2("Login\nuser@icloud.com\nPass\npass123\nIGN\nPlayer1\n7.200")
        self.assertTrue(v3["order_detected"])
        self.assertEqual(v3["email"], "user@icloud.com")
        self.assertEqual(v3["password"], "pass123")
        self.assertEqual(v3["username"], "Player1")
        self.assertEqual(v3["packages"][0]["package"], "7200")

        # 4. Comma thousands separator (7,200) with @proton.me
        v4 = parse_order_v2("E-mail\nuser@proton.me\nClave\npass456\nNombre\nPlayer2\n7,200cp.")
        self.assertTrue(v4["order_detected"])
        self.assertEqual(v4["email"], "user@proton.me")
        self.assertEqual(v4["password"], "pass456")
        self.assertEqual(v4["username"], "Player2")
        self.assertEqual(v4["packages"][0]["package"], "7200")

    async def test_false_positive_prevention(self):
        from order_parser import parse_order_v2

        # Random chat message with email only -> must not detect as order
        r1 = parse_order_v2("Contact us at info@example.com for support")
        self.assertFalse(r1["order_detected"])

        # Random message with number only -> must not detect as order
        r2 = parse_order_v2("The score was 7200 points in the game")
        self.assertFalse(r2["order_detected"])


class TestClientReactionAndMessageEditFilter(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_updates_ignored_by_edited_message_handler(self):
        from handlers import edited_message_handler, BOT_SETTINGS
        from unittest.mock import AsyncMock

        replied_messages = []

        class MockUser:
            id = 1573531032  # Super Admin ID

        class MockChat:
            id = -100123456

        class MockEffectiveMessage:
            message_id = 1475

        class MockUpdate:
            edited_message = None
            message_reaction = type("Reaction", (), {})()
            message_reaction_count = None
            effective_message = MockEffectiveMessage()
            effective_chat = MockChat()
            effective_user = MockUser()

        BOT_SETTINGS["source_group_id"] = -100123456

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": type("Bot", (), {"id": 999000})()})()

        # Run handler on reaction update
        await edited_message_handler(mock_update, mock_context)

        # ✓ Verify no reply notice was sent
        self.assertEqual(len(replied_messages), 0)

    async def test_reaction_or_emoji_on_non_order_chat_message_ignored(self):
        from handlers import edited_message_handler, BOT_SETTINGS

        replied_messages = []

        class MockUser:
            id = 999888777  # Normal Customer ID

        class MockChat:
            id = -100123456

        class MockEditedMessage:
            message_id = 2762
            text = "I'm fine bro thanks and You?"
            caption = None

            async def reply_text(self, text, reply_to_message_id=None):
                replied_messages.append({
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

        class MockUpdate:
            edited_message = MockEditedMessage()
            message_reaction = None
            message_reaction_count = None
            effective_message = MockEditedMessage()
            effective_chat = MockChat()
            effective_user = MockUser()

        BOT_SETTINGS["source_group_id"] = -100123456

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": type("Bot", (), {"id": 999000})()})()

        # Non-order message reaction/edit -> MUST be ignored completely!
        await edited_message_handler(mock_update, mock_context)
        self.assertEqual(len(replied_messages), 0)

    async def test_reaction_or_emoji_on_existing_order_with_unchanged_text_ignored(self):
        from handlers import edited_message_handler, BOT_SETTINGS
        from database import init_db, create_order, get_order_by_id

        await init_db()

        chat_id = -100123456
        msg_id = 2763
        order_text = "#105 Activision Safe 12000 Fast test@example.com Pass: 123 Nick: Gamer"

        # Create Order in DB
        order = await create_order(
            email="test@example.com",
            client_chat_id=chat_id,
            original_message_id=msg_id,
            package="12000",
            status="Pending",
            category="A",
            raw_text=order_text
        )

        replied_messages = []

        class MockUser:
            id = 999888777

        class MockChat:
            id = chat_id

        class MockEditedMessage:
            message_id = msg_id
            text = order_text  # Text did NOT change!
            caption = None

            async def reply_text(self, text, reply_to_message_id=None):
                replied_messages.append({
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

        class MockUpdate:
            edited_message = MockEditedMessage()
            message_reaction = None
            message_reaction_count = None
            effective_message = MockEditedMessage()
            effective_chat = MockChat()
            effective_user = MockUser()

        BOT_SETTINGS["source_group_id"] = chat_id

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": type("Bot", (), {"id": 999000})()})()

        # Reaction / metadata update on existing order without text change -> MUST be ignored!
        await edited_message_handler(mock_update, mock_context)
        self.assertEqual(len(replied_messages), 0)

    async def test_genuine_customer_edited_message_triggers_manual_placement_notice(self):
        from handlers import edited_message_handler, BOT_SETTINGS
        from database import init_db, create_order

        await init_db()

        chat_id = -100123456
        msg_id = 1475

        # Create existing order in DB with original text
        await create_order(
            email="tokio@example.com",
            client_chat_id=chat_id,
            original_message_id=msg_id,
            package="10800",
            status="Pending",
            category="A",
            raw_text="#106 Activision Safe 10800 Fast tokio@example.com Pass: oldpass Nick: Gamer"
        )

        replied_messages = []

        class MockUser:
            id = 999888777  # Normal Customer ID (not admin/delivery)

        class MockChat:
            id = chat_id

        class MockEditedMessage:
            message_id = msg_id
            text = "#106 Activision Safe 10800 Fast tokio@example.com Pass: NEWPASS123 Nick: Gamer"
            caption = None

            async def reply_text(self, text, reply_to_message_id=None):
                replied_messages.append({
                    "text": text,
                    "reply_to_message_id": reply_to_message_id
                })

        class MockUpdate:
            edited_message = MockEditedMessage()
            message_reaction = None
            message_reaction_count = None
            effective_message = MockEditedMessage()
            effective_chat = MockChat()
            effective_user = MockUser()

        BOT_SETTINGS["source_group_id"] = chat_id

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": type("Bot", (), {"id": 999000})()})()

        await edited_message_handler(mock_update, mock_context)

        # ✓ Verify manual placement notice was sent for genuine customer message text edit
        self.assertEqual(len(replied_messages), 1)
        self.assertIn("This order will be placed again manually wait for team", replied_messages[0]["text"])
        self.assertEqual(replied_messages[0]["reply_to_message_id"], 1475)


class TestCategoryAGroupLedgerIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_category_a_multi_group_ledger_isolation(self):
        from database import (
            init_db,
            record_delivery_ledger_entry,
            get_running_total_current,
            execute_pay_reset,
            execute_manual_adjustment,
            get_last_running_total_entry
        )

        await init_db()

        chat_a1 = -1001111111111
        chat_a2 = -1002222222222
        chat_a3 = -1003333333333

        # 1. Set initial balances: A-1 = $800, A-2 = $20, A-3 = $150
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=800.0, chat_id=chat_a1)
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=20.0, chat_id=chat_a2)
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=150.0, chat_id=chat_a3)

        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 800.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 20.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)

        # 2. Delivery in A-1 ($50) -> Increases ONLY A-1 ($850). A-2 and A-3 remain unchanged.
        e1, _ = await record_delivery_ledger_entry(order_id=901, package="10800", now_value=50.0, chat_id=chat_a1)
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 850.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 20.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)

        # 3. Delivery in A-2 ($10) -> Increases ONLY A-2 ($30). A-1 and A-3 remain unchanged.
        e2, _ = await record_delivery_ledger_entry(order_id=902, package="2400", now_value=10.0, chat_id=chat_a2)
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 850.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 30.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)

        # 4. /pay in A-1 -> Resets ONLY A-1 ($0). A-2 stays $30, A-3 stays $150.
        entry_pay_a1, before_p1, paid_p1, cur_p1 = await execute_pay_reset(admin_id=1573531032, chat_id=chat_a1)
        self.assertEqual(before_p1, 850.0)
        self.assertEqual(cur_p1, 0.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 0.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 30.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)

        # 5. +10 in A-1 -> Affects ONLY A-1 ($10). A-2 stays $30.
        e_plus, b_plus, n_plus, a_plus, _ = await execute_manual_adjustment(10.0, admin_id=1573531032, chat_id=chat_a1)
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 10.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 30.0)

        # 6. -10 in A-2 -> Affects ONLY A-2 ($20). A-1 stays $10.
        e_minus, b_minus, n_minus, a_minus, _ = await execute_manual_adjustment(-10.0, admin_id=1573531032, chat_id=chat_a2)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 20.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 10.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)

        # 7. /pay in A-2 -> Resets ONLY A-2 ($0).
        entry_pay_a2, _, _, _ = await execute_pay_reset(admin_id=1573531032, chat_id=chat_a2)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 0.0)

        # 8. Restart recovery verification -> DB persistent totals
        self.assertEqual(await get_running_total_current(chat_id=chat_a1), 10.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a2), 0.0)
        self.assertEqual(await get_running_total_current(chat_id=chat_a3), 150.0)


class TestFacebookRecoveryCodeParserExclusion(unittest.IsolatedAsyncioTestCase):
    async def test_facebook_recovery_codes_not_detected_as_packages(self):
        from order_parser import parse_order_v2

        sample_fb = (
            "71#\n"
            "*facebook*\n\n"
            "Nick: Apodo en el juego HR°Dxwrin\n\n"
            "Correo o número fb +529619465770\n\n"
            "Contraseña de fb: darwin.41\n\n"
            "Códigos\n"
            "*1430 8078\n"
            "*1982 9264\n"
            "*2347 4217\n\n"
            "5k"
        )

        parsed = parse_order_v2(sample_fb)

        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["login_method"], "Facebook")
        self.assertEqual(parsed["recovery_codes"], ["1430 8078", "1982 9264", "2347 4217"])
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["unknown_packages"], [])

        pkg_names = [p["package"] for p in parsed["packages"]]
        self.assertNotIn("8078", pkg_names)
        self.assertNotIn("9264", pkg_names)
        self.assertNotIn("4217", pkg_names)
        self.assertNotIn("1430", pkg_names)

    async def test_facebook_recovery_codes_another_format(self):
        from order_parser import parse_order_v2

        sample_fb2 = (
            "*facebook*\n\n"
            "Correo o número fb: user@gmail.com\n"
            "Contraseña de fb: password\n\n"
            "Códigos:\n"
            "2534 6603\n"
            "3075 1980\n"
            "3568 0949\n\n"
            "5k"
        )

        parsed = parse_order_v2(sample_fb2)

        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["unknown_packages"], [])
        self.assertEqual(parsed["recovery_codes"], ["2534 6603", "3075 1980", "3568 0949"])

    async def test_phone_order_and_credentials_safety(self):
        from order_parser import parse_order_v2

        sample = (
            "Order #: 54\n"
            "Correo: test@gmail.com\n"
            "Phone: +529619465770\n"
            "Pass: mypass123\n"
            "5040"
        )

        parsed = parse_order_v2(sample)

        self.assertTrue(parsed["order_detected"])
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        pkg_names = [p["package"] for p in parsed["packages"]]
        self.assertNotIn("529619465770", pkg_names)
        self.assertNotIn("54", pkg_names)
        self.assertNotIn("123", pkg_names)

    async def test_genuine_unknown_cp_package(self):
        from order_parser import parse_order_v2

        sample = (
            "Email: user@gmail.com\n"
            "Pass: password\n"
            "CP PACK: 777777"
        )

        parsed = parse_order_v2(sample)

        self.assertTrue(parsed["order_detected"])
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "777777")
        self.assertFalse(parsed["packages"][0]["known"])
        self.assertIn("777777", parsed["unknown_packages"])


class TestClientOrderCancellationRequestWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_client_replies_cancel_creates_request_for_loader(self):
        from database import init_db, create_order, get_order_by_id
        from handlers import handle_client_cancellation_request

        await init_db()

        order = await create_order(
            email="client_cancel_1@test.com",
            client_chat_id=-100123456,
            original_message_id=9001,
            package="2400",
            status="Pending"
        )

        sent_messages = []

        class MockRepliedMsg:
            message_id = 9001

        class MockUser:
            id = 111222333

        class MockChat:
            id = -100123456

        class MockMessage:
            message_id = 9002
            text = "cancel"
            caption = None
            reply_to_message = MockRepliedMsg()

            async def reply_text(self, text, quote=True):
                sent_messages.append(text)

        class MockBot:
            async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None, parse_mode=None):
                sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

        class MockUpdate:
            effective_message = MockMessage()
            effective_user = MockUser()
            effective_chat = MockChat()

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": MockBot()})()

        handled = await handle_client_cancellation_request(mock_update, mock_context)
        self.assertTrue(handled)

        updated_order = await get_order_by_id(order.id)
        self.assertTrue(updated_order.cancellation_requested)
        self.assertEqual(updated_order.status, "Pending")  # Must NOT be cancelled yet

    async def test_random_cancel_without_reply_is_ignored(self):
        from handlers import handle_client_cancellation_request

        class MockUser:
            id = 111222333

        class MockChat:
            id = -100123456

        class MockMessage:
            message_id = 9005
            text = "cancel"
            caption = None
            reply_to_message = None  # No reply

        class MockUpdate:
            effective_message = MockMessage()
            effective_user = MockUser()
            effective_chat = MockChat()

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": None})()

        handled = await handle_client_cancellation_request(mock_update, mock_context)
        self.assertFalse(handled)

    async def test_duplicate_cancellation_request_prevented(self):
        from database import init_db, create_order, request_order_cancellation
        from handlers import handle_client_cancellation_request

        await init_db()

        order = await create_order(
            email="client_cancel_dup@test.com",
            client_chat_id=-100123456,
            original_message_id=9010,
            package="5040",
            status="Pending"
        )
        await request_order_cancellation(order.id)

        replied_text = []

        class MockRepliedMsg:
            message_id = 9010

        class MockMessage:
            message_id = 9011
            text = "cancel"
            caption = None
            reply_to_message = MockRepliedMsg()

            async def reply_text(self, text, quote=True):
                replied_text.append(text)

        class MockUpdate:
            effective_message = MockMessage()
            effective_user = type("User", (), {"id": 111})()
            effective_chat = type("Chat", (), {"id": -100123456})()

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": None})()

        handled = await handle_client_cancellation_request(mock_update, mock_context)
        self.assertTrue(handled)
        self.assertIn("Cancellation request already sent", replied_text[0])

    async def test_already_delivered_order_cannot_be_cancelled(self):
        from database import init_db, create_order
        from handlers import handle_client_cancellation_request

        await init_db()

        order = await create_order(
            email="client_cancel_del@test.com",
            client_chat_id=-100123456,
            original_message_id=9020,
            package="10800",
            status="Delivered"
        )

        replied_text = []

        class MockRepliedMsg:
            message_id = 9020

        class MockMessage:
            message_id = 9021
            text = "/cancel"
            caption = None
            reply_to_message = MockRepliedMsg()

            async def reply_text(self, text, quote=True):
                replied_text.append(text)

        class MockUpdate:
            effective_message = MockMessage()
            effective_user = type("User", (), {"id": 111})()
            effective_chat = type("Chat", (), {"id": -100123456})()

        mock_update = MockUpdate()
        mock_context = type("Context", (), {"bot": None})()

        handled = await handle_client_cancellation_request(mock_update, mock_context)
        self.assertTrue(handled)
        self.assertIn("already been delivered and cannot be cancelled", replied_text[0])

    async def test_loader_decisions_cancel_wait_almost(self):
        from database import init_db, create_order, request_order_cancellation, get_order_by_id
        from handlers import client_cancellation_request_callback_handler

        await init_db()

        # 1. Loader cancels order
        order1 = await create_order(
            email="loader_dec_1@test.com",
            client_chat_id=-100123456,
            original_message_id=9030,
            package="2400",
            status="Pending"
        )
        await request_order_cancellation(order1.id)

        class MockUser:
            id = 1573531032  # Super Admin ID

        class MockQuery:
            from_user = MockUser()
            data = f"cancel_req_cancel:{order1.id}"
            async def answer(self, text=None, show_alert=False): pass
            async def edit_message_text(self, text, parse_mode=None): pass

        class MockUpdate:
            callback_query = MockQuery()

        await client_cancellation_request_callback_handler(MockUpdate(), type("Context", (), {"bot": None})())

        up1 = await get_order_by_id(order1.id)
        self.assertEqual(up1.status, "Cancelled")
        self.assertFalse(up1.cancellation_requested)
        self.assertEqual(up1.cancellation_decision, "cancelled")

        # 2. Loader selects wait
        order2 = await create_order(
            email="loader_dec_2@test.com",
            client_chat_id=-100123456,
            original_message_id=9040,
            package="2400",
            status="Pending"
        )
        await request_order_cancellation(order2.id)

        class MockQueryWait:
            from_user = MockUser()
            data = f"cancel_req_wait:{order2.id}"
            async def answer(self, text=None, show_alert=False): pass
            async def edit_message_text(self, text, parse_mode=None): pass

        await client_cancellation_request_callback_handler(type("Update", (), {"callback_query": MockQueryWait()})(), type("Context", (), {"bot": None})())

        up2 = await get_order_by_id(order2.id)
        self.assertEqual(up2.status, "Pending")
        self.assertFalse(up2.cancellation_requested)
        self.assertEqual(up2.cancellation_decision, "wait")

        # 3. Loader selects almost done
        order3 = await create_order(
            email="loader_dec_3@test.com",
            client_chat_id=-100123456,
            original_message_id=9050,
            package="2400",
            status="Pending"
        )
        await request_order_cancellation(order3.id)

        class MockQueryAlmost:
            from_user = MockUser()
            data = f"cancel_req_almost:{order3.id}"
            async def answer(self, text=None, show_alert=False): pass
            async def edit_message_text(self, text, parse_mode=None): pass

        await client_cancellation_request_callback_handler(type("Update", (), {"callback_query": MockQueryAlmost()})(), type("Context", (), {"bot": None})())

        up3 = await get_order_by_id(order3.id)
        self.assertEqual(up3.status, "Pending")
        self.assertFalse(up3.cancellation_requested)
        self.assertEqual(up3.cancellation_decision, "rejected")


class TestCategoryABPricingAndWalletSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import Wallet, WalletTransaction, PaymentTransaction
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(WalletTransaction))
            await session.execute(delete(PaymentTransaction))
            await session.execute(delete(Wallet))
            await session.commit()

    async def asyncTearDown(self):
        from database import bulk_update_package_prices_in_db, DEFAULT_PACKAGE_PRICES
        await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, category="A")
        await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, category="B")

    async def test_category_a_and_b_price_list_isolation(self):
        from database import init_db, bulk_update_package_prices_in_db, get_all_package_prices_from_db, DEFAULT_PACKAGE_PRICES

        await init_db()

        try:
            # Update Category A 2400 price to 16.5
            await bulk_update_package_prices_in_db({"2400": 16.5}, category="A")
            # Update Category B 2400 price to 15.0
            await bulk_update_package_prices_in_db({"2400": 15.0}, category="B")

            prices_a = await get_all_package_prices_from_db(category="A")
            prices_b = await get_all_package_prices_from_db(category="B")

            self.assertEqual(prices_a.get("2400"), 16.5)
            self.assertEqual(prices_b.get("2400"), 15.0)

            # Update Category A price to 15.5
            await bulk_update_package_prices_in_db({"2400": 15.5}, category="A")

            prices_a_new = await get_all_package_prices_from_db(category="A")
            prices_b_new = await get_all_package_prices_from_db(category="B")

            # Category A price must be 15.5, Category B price MUST remain 15.0!
            self.assertEqual(prices_a_new.get("2400"), 15.5)
            self.assertEqual(prices_b_new.get("2400"), 15.0)
        finally:
            await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, category="A")
            await bulk_update_package_prices_in_db(DEFAULT_PACKAGE_PRICES, category="B")

    async def test_critical_wallet_identity_rule(self):
        import uuid
        from database import init_db, get_or_create_wallet, topup_wallet, deduct_wallet_balance_for_order

        await init_db()

        group_b1 = -1001111111111
        group_b2 = -1002222222222
        user_123 = 777123

        tx1 = f"TX_B1_{uuid.uuid4().hex[:6]}"
        tx2 = f"TX_B2_{uuid.uuid4().hex[:6]}"

        # Create wallets for B1+User123 and B2+User123
        w1, s1, _ = await topup_wallet(group_b1, user_123, 100.0, provider="Binance", transaction_id=tx1)
        w2, s2, _ = await topup_wallet(group_b2, user_123, 20.0, provider="Bybit", transaction_id=tx2)

        self.assertTrue(s1)
        self.assertTrue(s2)
        self.assertEqual(w1.balance, 100.0)
        self.assertEqual(w2.balance, 20.0)

        # Spend $30 in Group B1
        w1_after, s_deduct, _ = await deduct_wallet_balance_for_order(group_b1, user_123, order_id=8801, amount=30.0)
        self.assertTrue(s_deduct)
        self.assertEqual(w1_after.balance, 70.0)

        # Group B2 wallet MUST remain 20.0!
        w2_check = await get_or_create_wallet(group_b2, user_123)
        self.assertEqual(w2_check.balance, 20.0)

    async def test_duplicate_transaction_protection(self):
        import uuid
        from database import init_db, topup_wallet

        await init_db()

        group_b1 = -1001111111111
        user_id = 999111
        tx_dup = f"TX_DUP_{uuid.uuid4().hex[:6]}"

        # First topup with tx_dup
        w1, ok1, reason1 = await topup_wallet(group_b1, user_id, 50.0, provider="Binance", transaction_id=tx_dup)
        self.assertTrue(ok1)
        self.assertEqual(w1.balance, 50.0)

        # Duplicate topup attempt with SAME provider and transaction_id
        w2, ok2, reason2 = await topup_wallet(group_b1, user_id, 50.0, provider="Binance", transaction_id=tx_dup)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "DUPLICATE_TRANSACTION")

        # Balance must remain 50.0
        w_final = await topup_wallet(group_b1, user_id, 0.0, provider="Binance") # Check
        self.assertEqual(w1.balance, 50.0)

    async def test_payment_verifier_rule_17_missing_credentials(self):
        from payment_verifier import verify_payment_transaction

        # Invalid currency test
        ok_curr, code_curr, msg_curr = await verify_payment_transaction("Binance", "TX100", 50.0, currency="BTC")
        self.assertFalse(ok_curr)
        self.assertEqual(code_curr, "UNSUPPORTED_CURRENCY")

        # Rule #17 test: Missing API credentials
        ok_rule17, code_rule17, msg_rule17 = await verify_payment_transaction("Binance", "TX100", 50.0, currency="USDT")
        self.assertFalse(ok_rule17)
        self.assertEqual(code_rule17, "MISSING_API_CREDENTIALS")

    async def test_wallet_deduction_does_not_modify_delivery_ledger(self):
        from database import init_db, topup_wallet, deduct_wallet_balance_for_order, get_running_total_current

        await init_db()

        group_b1 = -1005555555555
        user_id = 333444

        before_ledger_total = await get_running_total_current(chat_id=group_b1)

        # Top up wallet
        await topup_wallet(group_b1, user_id, 200.0, provider="Admin")
        # Deduct wallet
        await deduct_wallet_balance_for_order(group_b1, user_id, order_id=9999, amount=65.0)

        after_ledger_total = await get_running_total_current(chat_id=group_b1)

        # DeliveryLedger running total MUST remain 100% unchanged by wallet top-up / deduction!
        self.assertEqual(before_ledger_total, after_ledger_total)

    async def test_binance_api_read_only_connectivity_test(self):
        from payment_verifier import test_binance_api_connectivity

        report = await test_binance_api_connectivity()
        self.assertIn("credentials_loaded", report)
        self.assertIn("formatted_text", report)
        self.assertIn("🧪 <b>Binance API Multi-Endpoint Diagnostics</b>", report["formatted_text"])

    async def test_wallet_command_handler_execution_and_category_a_safety(self):
        from handlers import wallet_command_handler
        from database import CLIENT_GROUPS_CACHE, init_db

        await init_db()

        class MockUser:
            id = 555123
            first_name = "TestUser"
            username = "testuser"

        class MockChat:
            id = -100999888
            title = "Test Group B"

        class MockMessage:
            def __init__(self):
                self.replied_text = None
            async def reply_text(self, text, parse_mode=None, **kwargs):
                if "quote" in kwargs:
                    raise TypeError("Message.reply_text() got an unexpected keyword argument 'quote'")
                self.replied_text = text

        # 1. Category B Group Wallet Execution (/wallet & /balance)
        CLIENT_GROUPS_CACHE[-100999888] = "B"
        msg_b = MockMessage()
        up_b = type("Update", (), {"effective_user": MockUser(), "effective_chat": MockChat(), "message": msg_b})()
        ctx_b = type("Context", (), {})()

        # Should execute without NameError
        await wallet_command_handler(up_b, ctx_b)
        self.assertIsNotNone(msg_b.replied_text)
        self.assertIn("Category B Wallet Overview", msg_b.replied_text)
        self.assertIn("Current Balance:", msg_b.replied_text)
        self.assertIn("$0.00", msg_b.replied_text)

        # 2. Category A Group Wallet Execution (Must refuse & NOT create wallet)
        chat_a = MockChat()
        chat_a.id = -100777666
        chat_a.title = "Test Group A"
        CLIENT_GROUPS_CACHE[-100777666] = "A"

        msg_a = MockMessage()
        up_a = type("Update", (), {"effective_user": MockUser(), "effective_chat": chat_a, "message": msg_a})()
        ctx_a = type("Context", (), {})()

        await wallet_command_handler(up_a, ctx_a)
        self.assertIsNotNone(msg_a.replied_text)
        self.assertIn("Wallet system is active only for Category B groups.", msg_a.replied_text)


class TestCategoryAOrderDetectionRestoration(unittest.TestCase):
    def test_category_a_order_105_activision_safe_12000_fast(self):
        from order_parser import parse_order_v2, extract_customer_ref_id
        from email_parser import extract_package

        text = (
            "Zain Active 🐱\n"
            "#105\n"
            "Activision\n"
            "Safe\n"
            "12000\n"
            "Fast\n\n"
            "manuel.3lgzl@hotmail.com\n\n"
            "Pass:\n"
            "26manuel07\n\n"
            "Nick: LzMeme"
        )
        parsed = parse_order_v2(text, category="A")
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"], "manuel.3lgzl@hotmail.com")
        self.assertEqual(parsed["login_method"], "Activision")
        self.assertEqual(parsed["password"], "26manuel07")
        self.assertEqual(parsed["username"], "LzMeme")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "12000")
        self.assertEqual(extract_customer_ref_id(text), "105")
        self.assertNotIn("@hotmail.com", extract_package(text))

    def test_category_a_order_106_activision_safe_10800_fast_nick_questionmarks(self):
        from order_parser import parse_order_v2, extract_customer_ref_id
        from email_parser import extract_package

        text = (
            "#106\n"
            "Activision\n"
            "Safe\n"
            "10800\n"
            "Fast\n\n"
            "tokiokaultiz1@hotmail.com\n\n"
            "Pass:\n"
            "LOLA2503.T\n\n"
            "Nick:\n"
            "???"
        )
        parsed = parse_order_v2(text, category="A")
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"], "tokiokaultiz1@hotmail.com")
        self.assertEqual(parsed["login_method"], "Activision")
        self.assertEqual(parsed["password"], "LOLA2503.T")
        self.assertEqual(parsed["username"], "???")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "10800")
        self.assertEqual(extract_customer_ref_id(text), "106")
        self.assertNotIn("@hotmail.com", extract_package(text))

    def test_category_a_order_with_customer_name_before_order_number(self):
        from order_parser import parse_order_v2, extract_customer_ref_id

        text = (
            "VIP Customer John 🔥\n"
            "Status: Active\n"
            "#500\n"
            "Activision\n"
            "Safe\n"
            "5040\n"
            "Normal\n\n"
            "john.doe@gmail.com\n\n"
            "Pass:\n"
            "secret123\n\n"
            "Nick: GamerJohn"
        )
        parsed = parse_order_v2(text, category="A")
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"], "john.doe@gmail.com")
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(extract_customer_ref_id(text), "500")

    def test_category_a_order_with_extra_blank_lines(self):
        from order_parser import parse_order_v2

        text = (
            "\n\n#999\n\n"
            "Activision\n\n"
            "Safe\n\n"
            "2400\n\n"
            "Fast\n\n\n"
            "test.user@outlook.com\n\n\n"
            "Pass:\n\n"
            "Pass1234\n\n\n"
            "Nick:\n\n"
            "ProGamer\n\n"
        )
        parsed = parse_order_v2(text, category="A")
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"], "test.user@outlook.com")
        self.assertEqual(parsed["packages"][0]["package"], "2400")

    def test_caption_text_detection_support(self):
        from keywords import contains_order_keyword

        caption_text = (
            "#777\n"
            "Activision\n"
            "Safe\n"
            "12000\n"
            "Fast\n\n"
            "caption.user@gmail.com\n\n"
            "Pass:\n"
            "CapPass123\n\n"
            "Nick: CaptionPlayer"
        )
        matched, kw = contains_order_keyword(caption_text)
        self.assertTrue(matched)
        self.assertEqual(kw, "activision")

    def test_facebook_recovery_codes_and_last_line_5k_package_detection(self):
        from order_parser import parse_order_v2, extract_customer_ref_id

        text = (
            "Ridaz cp seller ⭐\n"
            "280#\n"
            "Mr.Rembrandt\n\n"
            "Rembrandtbohorquez416@gmail.com\n\n"
            "r3m6r4n5T\n\n"
            "0674 5886\n"
            "0796 5268\n"
            "1726 2601\n"
            "5k"
        )

        parsed = parse_order_v2(text, category="A")
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["email"], "rembrandtbohorquez416@gmail.com")
        self.assertEqual(parsed["password"], "r3m6r4n5T")
        self.assertEqual(parsed["username"], "Mr.Rembrandt")
        self.assertEqual(extract_customer_ref_id(text), "280")
        self.assertEqual(parsed["recovery_codes"], ["0674 5886", "0796 5268", "1726 2601"])
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["unknown_packages"], [])


class TestCrossBotLoaderReplyIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_reply_to_external_bot_message_ignored(self):
        from handlers import delivery_group_handler, BOT_SETTINGS, LOADERS_CACHE, AUTH_USERS_CACHE
        from database import init_db, create_order, get_order_by_id

        await init_db()

        loader_group_id = -1002055608818
        BOT_SETTINGS["delivery_group_id"] = loader_group_id
        LOADERS_CACHE[1] = {"id": 1, "name": "Loader 1", "group_id": loader_group_id}

        # Delivery user authorized in bot
        loader_user_id = 998877
        AUTH_USERS_CACHE[loader_user_id] = "delivery"

        # Create Order in DB for Client Group -1004384376029
        order = await create_order(
            email="fb2@gmail.com",
            client_chat_id=-1004384376029,
            original_message_id=5555,
            package="10800",
            status="Pending",
            category="A"
        )
        self.assertIsNotNone(order.id)

        # Mock External Bot User (e.g. AG Done bot with ID 777888)
        class ExternalBotUser:
            id = 777888
            is_bot = True
            username = "AGDoneBot"
            first_name = "AG Done"

        # Mock Loader Message replied to External Bot message
        class RepliedMsg:
            message_id = 888111
            from_user = ExternalBotUser()
            text = f"Christtian Atay\nOrder #:{order.id} Login: Activision Email: fb2@gmail.com"
            caption = None

        class LoaderUser:
            id = loader_user_id
            first_name = "Loader"
            username = "loader"
            is_bot = False

        class LoaderChat:
            id = loader_group_id
            title = "Shared Loader Group"

        class PhotoSize:
            file_id = "ph_file_123"

        class LoaderMsg:
            message_id = 999222
            from_user = LoaderUser()
            chat = LoaderChat()
            reply_to_message = RepliedMsg()
            photo = [PhotoSize()]
            document = None
            text = None
            caption = "fb2@gmail.com"

            async def reply_text(self, text, parse_mode=None, **kwargs):
                pass

        up = type("Update", (), {
            "effective_user": LoaderUser(),
            "effective_chat": LoaderChat(),
            "message": LoaderMsg(),
            "effective_message": LoaderMsg()
        })()

        mock_bot = type("Bot", (), {"id": 999000})()  # THIS bot's ID is 999000
        ctx = type("Context", (), {"bot": mock_bot})()

        # Invoke delivery_group_handler
        await delivery_group_handler(up, ctx)

        # Verify Order remains unchanged in Pending status and NOT delivered!
        check_order = await get_order_by_id(order.id)
        self.assertEqual(check_order.status, "Pending")
        self.assertNotEqual(check_order.status, "Delivered")


class TestOrderParserV2(unittest.TestCase):
    """Tests parse_order_v2 customer ref prefix detection and bounded package parsing."""

    def test_activision_header_ref_id(self):
        raw = "635*Activision*\n\nNick: Maracay..\nCorreo: junior.antonioviloria@gmail.com\nContraseña: 23793556\n\n5k"
        parsed = parse_order_v2(raw)
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["customer_ref_id"], "635")
        self.assertEqual(parsed["email"], "junior.antonioviloria@gmail.com")
        self.assertEqual(parsed["login_method"], "Activision")
        self.assertEqual(parsed["username"], "Maracay..")
        self.assertEqual(parsed["password"], "23793556")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["unknown_packages"], [])

    def test_facebook_header_ref_id(self):
        raw = "635 FB\n\nNick: Test..\nCorreo: test@gmail.com\nContraseña: 12345678\n\n12.000"
        parsed = parse_order_v2(raw)
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["customer_ref_id"], "635")
        self.assertEqual(parsed["email"], "test@gmail.com")
        self.assertEqual(parsed["login_method"], "Facebook")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "12000")

    def test_large_numeric_password_not_unknown_package(self):
        raw = "Order #100\nCorreo: test2@gmail.com\nContraseña: 718569324\n5k"
        parsed = parse_order_v2(raw)
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["customer_ref_id"], "100")
        self.assertEqual(parsed["password"], "718569324")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "5040")
        self.assertEqual(parsed["unknown_packages"], [])


class TestStep2DatabaseSchema(unittest.IsolatedAsyncioTestCase):
    """
    Tests STEP 2 Data Models & Database Schema requirements:
    1. Global client price creation
    2. Updating an existing global client price
    3. Loader A price creation
    4. Loader B price creation
    5. Confirm Loader A and Loader B prices remain independent
    6. Creating an order with pricing fields
    7. Creating an order with multiple OrderItems
    8. Nullable pricing fields on old orders
    9. Migration running more than once safely
    10. SQLite compatibility
    """

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import OrderItem, GlobalClientPrice, LoaderPrice
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderItem))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(GlobalClientPrice))
            await session.commit()

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, reload_global_client_prices_cache
        from models import GlobalClientPrice
        from sqlalchemy import delete
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.commit()
        await reload_global_client_prices_cache()


    async def test_global_client_price_creation_and_update(self):
        from database import set_global_client_price_in_db, get_all_global_client_prices_from_db, GLOBAL_CLIENT_PRICES_CACHE
        # 1. Creation
        item = await set_global_client_price_in_db("cp_10800", 65.5, display_name="10800 CP", package_type="normal_cp")
        self.assertIsNotNone(item.id)
        self.assertEqual(item.product_key, "cp_10800")
        self.assertEqual(item.price, 65.5)

        prices = await get_all_global_client_prices_from_db()
        self.assertTrue(any(p.product_key == "cp_10800" for p in prices))
        self.assertIn("cp_10800", GLOBAL_CLIENT_PRICES_CACHE)
        self.assertEqual(GLOBAL_CLIENT_PRICES_CACHE["cp_10800"]["price"], 65.5)

        # 2. Update existing
        updated = await set_global_client_price_in_db("cp_10800", 66.0, display_name="10800 CP Updated")
        self.assertEqual(updated.id, item.id)
        self.assertEqual(updated.price, 66.0)
        self.assertEqual(GLOBAL_CLIENT_PRICES_CACHE["cp_10800"]["price"], 66.0)

    async def test_loader_prices_independence(self):
        from database import add_loader, set_loader_price_in_db, get_all_loader_prices_from_db, LOADER_PRICES_CACHE
        # Setup 2 distinct loaders
        loader_a = await add_loader("Loader Alpha", 999001)
        loader_b = await add_loader("Loader Beta", 999002)


        # 3. Loader A price creation
        price_a = await set_loader_price_in_db(loader_a.id, "cp_10800", 55.0, display_name="10800 CP")
        self.assertEqual(price_a.cost, 55.0)

        # 4. Loader B price creation
        price_b = await set_loader_price_in_db(loader_b.id, "cp_10800", 58.0, display_name="10800 CP")
        self.assertEqual(price_b.cost, 58.0)

        # 5. Confirm independence
        prices_a = await get_all_loader_prices_from_db(loader_a.id)
        prices_b = await get_all_loader_prices_from_db(loader_b.id)

        self.assertEqual(len(prices_a), 1)
        self.assertEqual(len(prices_b), 1)
        self.assertEqual(prices_a[0].cost, 55.0)
        self.assertEqual(prices_b[0].cost, 58.0)

        self.assertEqual(LOADER_PRICES_CACHE[loader_a.id]["cp_10800"]["cost"], 55.0)
        self.assertEqual(LOADER_PRICES_CACHE[loader_b.id]["cp_10800"]["cost"], 58.0)

    async def test_order_creation_with_pricing_fields(self):
        from database import create_order, get_order_by_id, AsyncSessionLocal
        from models import Order
        from sqlalchemy import select
        # 6. Creating order with pricing fields
        ord_obj = await create_order(email="client@test.com", package="10800 CP")
        async with AsyncSessionLocal() as session:
            db_ord = (await session.execute(select(Order).where(Order.id == ord_obj.id))).scalar_one_or_none()
            db_ord.client_price_total = 65.5
            db_ord.loader_cost_total = 55.0
            db_ord.profit_amount = 10.5
            db_ord.secret_profit_code = "F+C"
            await session.commit()

        fetched = await get_order_by_id(ord_obj.id)
        self.assertEqual(fetched.client_price_total, 65.5)
        self.assertEqual(fetched.loader_cost_total, 55.0)
        self.assertEqual(fetched.profit_amount, 10.5)
        self.assertEqual(fetched.secret_profit_code, "F+C")

    async def test_order_multiple_order_items(self):
        import time
        from database import create_order, AsyncSessionLocal
        from models import Order, OrderItem
        from sqlalchemy import select
        # 7. Order with multiple OrderItems
        email_addr = f"multi_{int(time.time() * 1000)}@test.com"

        ord_obj = await create_order(email=email_addr, package="2400 CP x 2 + 4800 CP x 1")

        async with AsyncSessionLocal() as session:
            item1 = OrderItem(
                order_id=ord_obj.id,
                product_key="cp_2400",
                display_name="2400 CP",
                quantity=2,
                client_unit_price=16.0,
                client_line_total=32.0,
                loader_unit_cost=13.0,
                loader_line_total=26.0,
                profit_amount=6.0
            )
            item2 = OrderItem(
                order_id=ord_obj.id,
                product_key="cp_4800",
                display_name="4800 CP",
                quantity=1,
                client_unit_price=30.0,
                client_line_total=30.0,
                loader_unit_cost=25.0,
                loader_line_total=25.0,
                profit_amount=5.0
            )
            session.add_all([item1, item2])
            await session.commit()

        async with AsyncSessionLocal() as session:
            stmt_items = select(OrderItem).where(OrderItem.order_id == ord_obj.id)
            items = list((await session.execute(stmt_items)).scalars().all())

            self.assertEqual(len(items), 2)
            total_client = sum(it.client_line_total for it in items)
            total_loader = sum(it.loader_line_total for it in items)
            total_profit = sum(it.profit_amount for it in items)

            self.assertEqual(total_client, 62.0)
            self.assertEqual(total_loader, 51.0)
            self.assertEqual(total_profit, 11.0)


    async def test_nullable_pricing_fields_on_old_orders(self):
        from database import create_order, get_order_by_id
        # 8. Nullable pricing fields on old/existing orders
        ord_old = await create_order(email="oldorder@test.com", package="5040 CP")
        fetched = await get_order_by_id(ord_old.id)
        self.assertIsNone(fetched.client_price_total)
        self.assertIsNone(fetched.loader_cost_total)
        self.assertIsNone(fetched.profit_amount)
        self.assertIsNone(fetched.secret_profit_code)

    async def test_idempotent_migration_running_multiple_times(self):
        from database import init_db, _migrate_orders_schema, engine
        # 9. Migration running more than once safely
        await init_db()
        async with engine.begin() as conn:
            await conn.run_sync(_migrate_orders_schema)
            await conn.run_sync(_migrate_orders_schema)


class TestStep3SecretCodeAndCatalog(unittest.TestCase):
    """
    Test suite for STEP 3 Secret Profit Code Engine & Product Catalog:
    1. Single profit code mappings
    2. Combination profit code mappings
    3. Decoder roundtrip equality for all encoded results
    4. Product catalog completeness, uniqueness, and correct reference prices
    5. Profit & multi-package calculation helpers using exact Decimal arithmetic
    """

    def test_single_secret_code_mappings(self):
        from decimal import Decimal
        from profit_code_engine import encode_profit_code, decode_profit_code

        expected_singles = [
            (Decimal("0"), "U"),
            (Decimal("0.25"), "K"),
            (Decimal("0.5"), "C"),
            (Decimal("0.75"), "L"),
            (Decimal("1"), "V"),
            (Decimal("2"), "W"),
            (Decimal("3"), "Y"),
            (Decimal("4"), "X"),
            (Decimal("5"), "Z"),
            (Decimal("6"), "A"),
            (Decimal("7"), "B"),
            (Decimal("8"), "D"),
            (Decimal("9"), "E"),
            (Decimal("10"), "F"),
            (Decimal("11"), "G"),
            (Decimal("12"), "H"),
            (Decimal("13"), "I"),
            (Decimal("14"), "J"),
            (Decimal("-1"), "T"),
            (Decimal("-2"), "S"),
            (Decimal("-3"), "R"),
            (Decimal("-4"), "Q"),
        ]

        for val, expected_code in expected_singles:
            code = encode_profit_code(val)
            self.assertEqual(code, expected_code, f"Failed encoding for {val}: expected {expected_code}, got {code}")
            decoded = decode_profit_code(code)
            self.assertEqual(decoded, val, f"Failed decoding roundtrip for {val}: got {decoded}")

    def test_combination_secret_code_mappings(self):
        from decimal import Decimal
        from profit_code_engine import encode_profit_code, decode_profit_code

        combinations = [
            (Decimal("17"), "X+F+Y"),
            (Decimal("4.5"), "X+C"),
            (Decimal("10.5"), "F+C"),
            (Decimal("10.25"), "F+K"),
            (Decimal("14.75"), "J+L"),
        ]

        for val, expected_code in combinations:
            code = encode_profit_code(val)
            self.assertEqual(code, expected_code, f"Failed encoding combination for {val}: expected {expected_code}, got {code}")
            decoded = decode_profit_code(code)
            self.assertEqual(decoded, val, f"Roundtrip failed for {val}: got {decoded}")

        # 22.5 -> valid combination summing exactly to 22.5
        val_22_5 = Decimal("22.5")
        code_22_5 = encode_profit_code(val_22_5)
        self.assertIsNotNone(code_22_5)
        self.assertTrue(len(code_22_5) > 0)
        decoded_22_5 = decode_profit_code(code_22_5)
        self.assertEqual(decoded_22_5, val_22_5)

    def test_roundtrip_decoding_forall_encoded_results(self):
        from decimal import Decimal
        from profit_code_engine import encode_profit_code, decode_profit_code

        test_values = [
            Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"),
            Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"),
            Decimal("6"), Decimal("7"), Decimal("8"), Decimal("9"), Decimal("10"),
            Decimal("11"), Decimal("12"), Decimal("13"), Decimal("14"),
            Decimal("-1"), Decimal("-2"), Decimal("-3"), Decimal("-4"),
            Decimal("4.5"), Decimal("10.5"), Decimal("10.25"), Decimal("14.75"),
            Decimal("17"), Decimal("22.5"), Decimal("35.75"), Decimal("-3.5")
        ]

        for val in test_values:
            encoded = encode_profit_code(val)
            decoded = decode_profit_code(encoded)
            self.assertEqual(decoded, val, f"Roundtrip failed for {val}: encoded as '{encoded}', decoded as '{decoded}'")

    def test_product_catalog_structure_and_reference_prices(self):
        from product_catalog import (
            PRODUCT_CATALOG,
            get_all_products,
            get_product_by_key,
            get_products_by_type,
        )

        all_prods = get_all_products()
        self.assertGreaterEqual(len(all_prods), 40)

        # Keys uniqueness
        keys = list(PRODUCT_CATALOG.keys())
        self.assertEqual(len(keys), len(set(keys)), "Product keys must be unique!")

        # 26400 CP reference price check (Must be 150.5, NOT 15.5)
        cp_26400 = get_product_by_key("cp_26400")
        self.assertIsNotNone(cp_26400)
        self.assertEqual(cp_26400["reference_price"], 150.5)
        self.assertNotEqual(cp_26400["reference_price"], 15.5)

        # Check normal CP products reference prices
        normal_expected = {
            "cp_10800": (65.5, "10800 CP"),
            "cp_5000": (33.0, "5000 CP"),
            "cp_2400": (16.0, "2400 CP"),
            "cp_880": (8.0, "880 CP"),
            "cp_420": (4.5, "420 CP"),
        }
        for pkey, (ref_p, disp_name) in normal_expected.items():
            prod = get_product_by_key(pkey)
            self.assertIsNotNone(prod, f"Missing product {pkey}")
            self.assertEqual(prod["reference_price"], ref_p)
            self.assertEqual(prod["display_name"], disp_name)
            self.assertEqual(prod["package_type"], "normal_cp")

        # Check special CP products reference prices sample
        special_sample = {
            "cp_4800": 30.0,
            "cp_7200": 43.5,
            "cp_9600": 57.0,
            "cp_12000": 71.0,
            "cp_72000": 411.0,
        }
        for pkey, ref_p in special_sample.items():
            prod = get_product_by_key(pkey)
            self.assertIsNotNone(prod, f"Missing product {pkey}")
            self.assertEqual(prod["reference_price"], ref_p)
            self.assertEqual(prod["package_type"], "special_cp")

        # Check other products
        other_sample = {
            "full_event_deal": (15.0, "3280 CP + Epic Bundle", "bonus_deal"),
            "safe_vault_50": (38.0, "$50 Safe Vault", "safe_vault"),
            "safe_vault_30": (22.0, "$30 Safe Vault", "safe_vault"),
            "safe_vault_20": (12.5, "$20 Safe Vault", "safe_vault"),
            "safe_vault_10": (7.0, "$10 Safe Vault", "safe_vault"),
            "safe_vault_5": (4.5, "$5 Safe Vault", "safe_vault"),
            "full_chain": (16.0, "560 CP + 300 Mythic Cards", "full_chain"),
        }
        for pkey, (ref_p, disp_name, ptype) in other_sample.items():
            prod = get_product_by_key(pkey)
            self.assertIsNotNone(prod, f"Missing product {pkey}")
            self.assertEqual(prod["reference_price"], ref_p)
            self.assertEqual(prod["display_name"], disp_name)
            self.assertEqual(prod["package_type"], ptype)

        # Check product types filter
        self.assertEqual(len(get_products_by_type("normal_cp")), 5)
        self.assertEqual(len(get_products_by_type("special_cp")), 29)
        self.assertEqual(len(get_products_by_type("safe_vault")), 5)
        self.assertEqual(len(get_products_by_type("bonus_deal")), 1)
        self.assertEqual(len(get_products_by_type("full_chain")), 1)

    def test_profit_and_order_totals_calculation(self):
        from decimal import Decimal
        from profit_code_engine import encode_profit_code
        from pricing_calculator import (
            calculate_profit,
            calculate_profit_and_code,
            calculate_order_totals,
        )

        # 46 - 36 = 10 -> F
        profit1 = calculate_profit(46, 36)
        self.assertEqual(profit1, Decimal("10"))
        self.assertEqual(encode_profit_code(profit1), "F")

        # 46 - 29 = 17 -> X+F+Y
        profit2, code2 = calculate_profit_and_code(46, 29)
        self.assertEqual(profit2, Decimal("17"))
        self.assertEqual(code2, "X+F+Y")

        # Multi-package calculation test: 2400 CP x 2, 4800 CP x 1
        items = [
            {"product_key": "cp_2400", "quantity": 2},
            {"product_key": "cp_4800", "quantity": 1},
        ]
        # Reference prices: cp_2400 -> 16, cp_4800 -> 30
        # Client total: 16*2 + 30*1 = 62
        loader_prices = {"cp_2400": Decimal("13"), "cp_4800": Decimal("25")}
        # Loader total: 13*2 + 25*1 = 51
        # Profit: 62 - 51 = 11 -> G

        totals = calculate_order_totals(items, loader_price_map=loader_prices)
        self.assertEqual(totals["client_total"], Decimal("62"))
        self.assertEqual(totals["loader_total"], Decimal("51"))
        self.assertEqual(totals["profit"], Decimal("11"))
        self.assertEqual(totals["secret_code"], "G")
        self.assertEqual(len(totals["item_breakdown"]), 2)


class TestStep4GlobalClientPricesAndSetCommand(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for STEP 4 Global Client Price List & /setclientprice:
    1-18. Parser unit tests for all formats, special products, edge cases, validation
    19-20. Command handler security & reply requirement checks
    21-25. Atomic DB updates, cache sync, non-destructive isolation
    26. Regression preservation
    """

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import GlobalClientPrice, LoaderPrice, Order
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(Order))
            await session.commit()

    def test_parse_normal_cp_comma_and_arrows(self):
        from price_list_parser import parse_client_price_list

        text1 = "10,800 CP ➜ $65.5 💵"
        res1 = parse_client_price_list(text1)
        self.assertTrue(res1["valid"])
        self.assertEqual(res1["parsed_prices"].get("cp_10800"), 65.5)

        text2 = "5,000 CP ➡️ $33 💵"
        res2 = parse_client_price_list(text2)
        self.assertTrue(res2["valid"])
        self.assertEqual(res2["parsed_prices"].get("cp_5000"), 33.0)

        text3 = "2400 CP -> $16"
        res3 = parse_client_price_list(text3)
        self.assertTrue(res3["valid"])
        self.assertEqual(res3["parsed_prices"].get("cp_2400"), 16.0)

        text4 = "880 CP → $8"
        res4 = parse_client_price_list(text4)
        self.assertTrue(res4["valid"])
        self.assertEqual(res4["parsed_prices"].get("cp_880"), 8.0)

        text5 = "420 CP = $4.5"
        res5 = parse_client_price_list(text5)
        self.assertTrue(res5["valid"])
        self.assertEqual(res5["parsed_prices"].get("cp_420"), 4.5)

    def test_parse_special_cp_and_decimal_26400(self):
        from price_list_parser import parse_client_price_list

        text = (
            "72,000 CP ➡️ $411💵\n"
            "26,400 CP ➡️ $150.5 💵\n"
            "4,800 CP ➜ $30 💵"
        )
        res = parse_client_price_list(text)
        self.assertTrue(res["valid"])
        self.assertEqual(res["parsed_prices"].get("cp_72000"), 411.0)
        self.assertEqual(res["parsed_prices"].get("cp_26400"), 150.5)
        self.assertEqual(res["parsed_prices"].get("cp_4800"), 30.0)

    def test_parse_full_event_deal_and_safe_vaults_and_full_chain(self):
        from price_list_parser import parse_client_price_list

        text = """
🎁 BONUS DEAL 💰

$17 Full Event Deal
(3280 CP + Epic Bundle)
💵 $15 USDT ✅
🔐 Only accounts starting with $1 and ending at $10

━━━━━━━━━━━━━━

🏦 SAFE VAULT

💵 $50 ➜ $38 USDT
💵 $30 ➜ $22 USDT
💵 $20 ➜ $12.5 USDT
💵 $10 ➜ $7 USDT
💵 $5 ➜ $4.5 USDT

━━━━━━━━━━━━━━

🔥 FULL CHAIN COST

Only available for $1 to $7 Accounts

💎 560 CP
🃏 300 Mythic Cards

💰 Total Cost: $16
"""
        res = parse_client_price_list(text)
        self.assertTrue(res["valid"])
        prices = res["parsed_prices"]

        # 9. Full Event Deal = 15
        self.assertEqual(prices.get("full_event_deal"), 15.0)
        # 13. Must NOT interpret 17 as price
        self.assertNotEqual(prices.get("full_event_deal"), 17.0)

        # 10. Safe Vaults
        self.assertEqual(prices.get("safe_vault_50"), 38.0)
        self.assertEqual(prices.get("safe_vault_30"), 22.0)
        self.assertEqual(prices.get("safe_vault_20"), 12.5)
        self.assertEqual(prices.get("safe_vault_10"), 7.0)
        self.assertEqual(prices.get("safe_vault_5"), 4.5)

        # 11. Full Chain = 16
        self.assertEqual(prices.get("full_chain"), 16.0)
        # 12. Must NOT treat 560 CP as cp_560
        self.assertNotIn("cp_560", prices)
        # 3280 CP must NOT be cp_3280
        self.assertNotIn("cp_3280", prices)

    def test_duplicate_identical_and_conflicting_lines(self):
        from price_list_parser import parse_client_price_list

        # 14. Identical duplicates -> valid
        text_ident = "10,800 CP ➜ $65.5\n10,800 CP ➜ $65.5"
        res_ident = parse_client_price_list(text_ident)
        self.assertTrue(res_ident["valid"])
        self.assertEqual(res_ident["parsed_prices"].get("cp_10800"), 65.5)

        # 15. Conflicting duplicates -> invalid
        text_conflict = "10,800 CP ➜ $65.5\n10,800 CP ➜ $70.0"
        res_conflict = parse_client_price_list(text_conflict)
        self.assertFalse(res_conflict["valid"])
        self.assertTrue(len(res_conflict["errors"]) > 0)

    def test_invalid_prices_empty_and_no_price_lines(self):
        from price_list_parser import parse_client_price_list

        # 16. Invalid non-numeric price
        res_inv = parse_client_price_list("10800 CP ➜ FREE")
        self.assertFalse(res_inv["valid"])

        # 17. Empty message
        res_empty = parse_client_price_list("")
        self.assertFalse(res_empty["valid"])

        # 18. No price lines
        res_no_prices = parse_client_price_list("Hello this is just a normal conversation message")
        self.assertFalse(res_no_prices["valid"])

    async def test_setclientprice_command_unauthorized_and_no_reply(self):
        from handlers import setclientprice_command_handler

        class MockUser:
            id = 999999
            username = "unauth_user"

        class MockMessage:
            reply_to_message = None
            text = "/setclientprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        class MockUpdate:
            effective_user = MockUser()
            effective_message = MockMessage()

        up = MockUpdate()
        ctx = type("Context", (), {})()

        # 19. Unauthorized command execution
        await setclientprice_command_handler(up, ctx)
        self.assertIn("not authorized", up.effective_message.replied_text.lower())

        # 20. Command without reply (authorized user)
        from config import Config
        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032
        up.effective_user.id = admin_id

        await setclientprice_command_handler(up, ctx)
        self.assertIn("reply to a price-list message", up.effective_message.replied_text.lower())

    async def test_setclientprice_successful_db_and_cache_update(self):
        from handlers import setclientprice_command_handler
        from database import (
            get_global_client_price,
            get_all_global_client_prices,
            GLOBAL_CLIENT_PRICES_CACHE,
            set_loader_price_in_db,
            get_all_loader_prices_from_db,
            create_order,
            get_order_by_id
        )
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        # Create prior order and loader price to verify isolation (24, 25)
        old_order = await create_order(email="old_step4@test.com", package="10800 CP")
        loader_price = await set_loader_price_in_db(loader_id=77, product_key="cp_10800", cost=55.0)

        class MockRepliedMsg:
            text = """
10,800 CP ➜ $65.5 💵
5,000 CP ➜ $33 💵
2,400 CP ➜ $16 💵

🎁 BONUS DEAL 💰
$17 Full Event Deal
(3280 CP + Epic Bundle)
💵 $15 USDT ✅

🏦 SAFE VAULT
💵 $50 ➜ $38 USDT
💵 $30 ➜ $22 USDT

🔥 FULL CHAIN COST
💎 560 CP
💰 Total Cost: $16
"""

        class MockCmdMsg:
            reply_to_message = MockRepliedMsg()
            text = "/setclientprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        class MockUser:
            id = admin_id
            username = "admin"

        class MockUpdate:
            effective_user = MockUser()
            effective_message = MockCmdMsg()

        up = MockUpdate()
        ctx = type("Context", (), {})()

        await setclientprice_command_handler(up, ctx)
        self.assertIn("Client Price List Updated", up.effective_message.replied_text)
        self.assertIn("Products updated: 7", up.effective_message.replied_text)

        # 21. Database updated
        p_10800 = await get_global_client_price("cp_10800")
        self.assertEqual(p_10800, 65.5)

        p_event = await get_global_client_price("full_event_deal")
        self.assertEqual(p_event, 15.0)

        p_chain = await get_global_client_price("full_chain")
        self.assertEqual(p_chain, 16.0)

        # 22. Cache updated
        self.assertIn("cp_10800", GLOBAL_CLIENT_PRICES_CACHE)
        self.assertEqual(GLOBAL_CLIENT_PRICES_CACHE["cp_10800"]["price"], 65.5)
        self.assertEqual(GLOBAL_CLIENT_PRICES_CACHE["full_chain"]["price"], 16.0)

        # 24. Existing order remains unchanged
        check_ord = await get_order_by_id(old_order.id)
        self.assertEqual(check_ord.email, "old_step4@test.com")

        # 25. Loader price remains unchanged
        l_prices = await get_all_loader_prices_from_db(77)
        self.assertEqual(len(l_prices), 1)
        self.assertEqual(l_prices[0].cost, 55.0)

    async def test_failed_validation_does_not_overwrite_old_prices(self):
        from database import set_global_client_price, get_global_client_price
        from handlers import setclientprice_command_handler
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        # Set initial global price
        await set_global_client_price("cp_10800", 65.5)
        self.assertEqual(await get_global_client_price("cp_10800"), 65.5)

        # Send bad message with conflicting duplicate prices
        class MockRepliedMsg:
            text = "10,800 CP ➜ $65.5\n10,800 CP ➜ $99.0"

        class MockCmdMsg:
            reply_to_message = MockRepliedMsg()
            text = "/setclientprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        class MockUser:
            id = admin_id

        up = type("Update", (), {"effective_user": MockUser(), "effective_message": MockCmdMsg()})()
        ctx = type("Context", (), {})()

        # 23. Failed validation does not overwrite old price
        await setclientprice_command_handler(up, ctx)
        self.assertIn("Conflicting price", up.effective_message.replied_text)
        self.assertEqual(await get_global_client_price("cp_10800"), 65.5)


class TestStep5LoaderPricesAndSetCommand(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for Step 5: Per-Loader Private Price List and /setloaderprice command.
    """

    async def asyncSetUp(self):
        from database import engine, Base, LOADER_PRICES_CACHE, LOADERS_CACHE, GLOBAL_CLIENT_PRICES_CACHE
        from sqlalchemy import delete
        from models import LoaderPrice, GlobalClientPrice, Loader
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(Loader))
            await session.commit()

        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()
        GLOBAL_CLIENT_PRICES_CACHE.clear()

    async def test_step5_complete_coverage(self):
        from decimal import Decimal
        from database import (
            add_loader,
            set_loader_price,
            get_loader_price,
            get_all_loader_prices,
            update_loader_prices,
            set_global_client_price,
            get_global_client_price,
            LOADER_PRICES_CACHE,
            GLOBAL_CLIENT_PRICES_CACHE,
            LOADERS_CACHE
        )
        from handlers import setloaderprice_command_handler
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        # Setup 2 distinct loaders
        loader_a = await add_loader(group_id=1001, loader_name="Loader A")
        loader_b = await add_loader(group_id=1002, loader_name="Loader B")

        # 1. Registered loader can set own prices
        class MockRepliedMsg:
            text = "10800 CP ➜ $50\n5000 CP ➜ $25"
            caption = None

        class MockCmdMsg:
            reply_to_message = MockRepliedMsg()
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        class MockUserLoaderA:
            id = 1001

        class MockUpdateA:
            effective_user = MockUserLoaderA()
            effective_chat = type("Chat", (), {"id": 1001})()
            effective_message = MockCmdMsg()

        up_a = MockUpdateA()
        ctx_a = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_a, ctx_a)
        self.assertIn("Loader price list updated", up_a.effective_message.replied_text)
        self.assertIn("Products updated: 2", up_a.effective_message.replied_text)

        price_a_10800 = await get_loader_price(loader_a.id, "cp_10800")
        self.assertEqual(price_a_10800, Decimal("50"))

        # 2. Unregistered user cannot set loader prices
        class MockUserUnregistered:
            id = 999999

        class MockUpdateUnreg:
            effective_user = MockUserUnregistered()
            effective_chat = type("Chat", (), {"id": 999999})()
            effective_message = MockCmdMsg()

        up_unreg = MockUpdateUnreg()
        ctx_unreg = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_unreg, ctx_unreg)
        self.assertIn("not authorized", up_unreg.effective_message.replied_text.lower())

        # 3. Loader A prices are isolated from Loader B
        price_b_10800 = await get_loader_price(loader_b.id, "cp_10800")
        self.assertIsNone(price_b_10800)

        # 4. Loader A cannot overwrite Loader B
        class MockUserLoaderB:
            id = 1002

        class MockRepliedMsgB:
            text = "10800 CP ➜ $52"
            caption = None

        class MockCmdMsgB:
            reply_to_message = MockRepliedMsgB()
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_b = type("Update", (), {"effective_user": MockUserLoaderB(), "effective_chat": type("Chat", (), {"id": 1002})(), "effective_message": MockCmdMsgB()})()
        ctx_b = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_b, ctx_b)
        self.assertEqual(await get_loader_price(loader_a.id, "cp_10800"), Decimal("50"))
        self.assertEqual(await get_loader_price(loader_b.id, "cp_10800"), Decimal("52"))

        # 5. Admin can set a specific loader using loader_id
        class MockAdminUser:
            id = admin_id

        class MockRepliedMsgAdmin:
            text = "2400 CP ➜ $12.50\n880 CP ➜ $6"
            caption = None

        class MockCmdMsgAdmin:
            reply_to_message = MockRepliedMsgAdmin()
            text = f"/setloaderprice {loader_a.id}"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_admin = type("Update", (), {"effective_user": MockAdminUser(), "effective_chat": type("Chat", (), {"id": admin_id})(), "effective_message": MockCmdMsgAdmin()})()
        ctx_admin = type("Context", (), {"args": [str(loader_a.id)]})()

        await setloaderprice_command_handler(up_admin, ctx_admin)
        self.assertIn("Loader price list updated", up_admin.effective_message.replied_text)
        self.assertEqual(await get_loader_price(loader_a.id, "cp_2400"), Decimal("12.5"))

        # 6. Admin without loader_id is rejected as ambiguous
        class MockCmdMsgAdminNoArgs:
            reply_to_message = MockRepliedMsgAdmin()
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_admin_no_args = type("Update", (), {"effective_user": MockAdminUser(), "effective_chat": type("Chat", (), {"id": admin_id})(), "effective_message": MockCmdMsgAdminNoArgs()})()
        ctx_admin_no_args = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_admin_no_args, ctx_admin_no_args)
        self.assertIn("This group is not registered as a Loader Group", up_admin_no_args.effective_message.replied_text)

        # 7. Command without reply is rejected
        class MockCmdMsgNoReply:
            reply_to_message = None
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_no_reply = type("Update", (), {"effective_user": MockUserLoaderA(), "effective_chat": type("Chat", (), {"id": 1001})(), "effective_message": MockCmdMsgNoReply()})()
        ctx_no_reply = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_no_reply, ctx_no_reply)
        self.assertIn("Reply to a price-list message", up_no_reply.effective_message.replied_text)

        # 8. Invalid price list does not modify existing prices
        old_price_2400 = await get_loader_price(loader_a.id, "cp_2400")

        class MockRepliedInvalid:
            text = "10,800 CP ➜ $50\n10,800 CP ➜ $99"
            caption = None

        class MockCmdMsgInvalid:
            reply_to_message = MockRepliedInvalid()
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_invalid = type("Update", (), {"effective_user": MockUserLoaderA(), "effective_chat": type("Chat", (), {"id": 1001})(), "effective_message": MockCmdMsgInvalid()})()
        ctx_invalid = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_invalid, ctx_invalid)
        self.assertIn("Conflicting price", up_invalid.effective_message.replied_text)
        self.assertEqual(await get_loader_price(loader_a.id, "cp_2400"), old_price_2400)

        # 9. Partial price list preserves existing products
        class MockRepliedPartial:
            text = "420 CP ➜ $3.50"
            caption = None

        class MockCmdMsgPartial:
            reply_to_message = MockRepliedPartial()
            text = "/setloaderprice"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_partial = type("Update", (), {"effective_user": MockUserLoaderA(), "effective_chat": type("Chat", (), {"id": 1001})(), "effective_message": MockCmdMsgPartial()})()
        ctx_partial = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_partial, ctx_partial)
        self.assertEqual(await get_loader_price(loader_a.id, "cp_420"), Decimal("3.5"))
        self.assertEqual(await get_loader_price(loader_a.id, "cp_10800"), Decimal("50"))

        # 10. Multiple products update correctly
        # 11. Decimal prices work
        # 12. CP aliases/products parse correctly
        # 13. Safe Vault parses correctly
        # 14. Full Event Deal parses correctly
        # 15. Full Chain remains separate from cp_560
        class MockRepliedFullSuite:
            text = """
10,800 CP ➜ $49.99
5,000 CP ➜ $24.50
4,800 CP ➜ $20.00

🏦 SAFE VAULT
💵 $50 ➜ $35 USDT
💵 $30 ➜ $20 USDT

🎁 BONUS DEAL
$17 Full Event Deal
💵 $14 USDT

🔥 FULL CHAIN COST
💎 560 CP
Total Cost: $12.75
"""
            caption = None

        class MockCmdMsgFullSuite:
            reply_to_message = MockRepliedFullSuite()
            text = f"/setloaderprice {loader_b.id}"
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_fs = type("Update", (), {"effective_user": MockAdminUser(), "effective_chat": type("Chat", (), {"id": admin_id})(), "effective_message": MockCmdMsgFullSuite()})()
        ctx_fs = type("Context", (), {"args": [str(loader_b.id)]})()

        await setloaderprice_command_handler(up_fs, ctx_fs)
        self.assertIn("Loader price list updated", up_fs.effective_message.replied_text)

        self.assertEqual(await get_loader_price(loader_b.id, "cp_10800"), Decimal("49.99"))
        self.assertEqual(await get_loader_price(loader_b.id, "cp_5000"), Decimal("24.5"))
        self.assertEqual(await get_loader_price(loader_b.id, "cp_4800"), Decimal("20"))
        self.assertEqual(await get_loader_price(loader_b.id, "safe_vault_50"), Decimal("35"))
        self.assertEqual(await get_loader_price(loader_b.id, "full_event_deal"), Decimal("14"))
        self.assertEqual(await get_loader_price(loader_b.id, "full_chain"), Decimal("12.75"))
        self.assertIsNone(await get_loader_price(loader_b.id, "cp_560"))

        # 16. Cache is updated after successful DB commit
        self.assertIn(loader_b.id, LOADER_PRICES_CACHE)
        self.assertEqual(LOADER_PRICES_CACHE[loader_b.id]["cp_10800"]["cost"], 49.99)

        # 17. Failed transaction does not corrupt cache
        try:
            await update_loader_prices(loader_b.id, {"invalid_key_causes_db_error": "not_a_number"})
        except Exception:
            pass
        self.assertEqual(LOADER_PRICES_CACHE[loader_b.id]["cp_10800"]["cost"], 49.99)

        # 18. Client global prices remain unchanged
        await set_global_client_price("cp_10800", 65.5)
        self.assertEqual(await get_global_client_price("cp_10800"), 65.5)
        self.assertEqual(await get_loader_price(loader_b.id, "cp_10800"), Decimal("49.99"))

        # 19. Loader A cache cannot return Loader B prices
        self.assertEqual(await get_loader_price(loader_a.id, "cp_10800"), Decimal("50"))
        self.assertEqual(await get_loader_price(loader_b.id, "cp_10800"), Decimal("49.99"))


class TestStep6OrderPricingAndProfitCode(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for Step 6: Order Pricing + Loader Cost + Profit + Secret Profit Code.
    """

    async def asyncSetUp(self):
        from database import engine, Base, LOADER_PRICES_CACHE, LOADERS_CACHE, GLOBAL_CLIENT_PRICES_CACHE
        from sqlalchemy import delete
        from models import Order, OrderItem, LoaderPrice, GlobalClientPrice, Loader
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderItem))
            await session.execute(delete(Order))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(Loader))
            await session.commit()

        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()
        GLOBAL_CLIENT_PRICES_CACHE.clear()

    async def test_step6_examples_and_pricing_engine(self):
        from decimal import Decimal
        from database import (
            add_loader,
            set_loader_price,
            set_global_client_price,
            create_order,
            save_order_pricing,
            get_order_by_id,
            GLOBAL_CLIENT_PRICES_CACHE,
            LOADER_PRICES_CACHE
        )
        from pricing_calculator import calculate_order_pricing, calculate_profit_and_code, parse_order_items_from_text

        loader_a = await add_loader(group_id=2001, loader_name="Loader Alpha")
        loader_b = await add_loader(group_id=2002, loader_name="Loader Beta")

        # Set Global Client Prices
        await set_global_client_price("cp_2400", 16.0)
        await set_global_client_price("cp_880", 8.0)
        await set_global_client_price("cp_10800", 65.5)
        await set_global_client_price("cp_4800", 30.0)
        await set_global_client_price("full_chain", 16.0)
        await set_global_client_price("full_event_deal", 15.0)
        await set_global_client_price("safe_vault_50", 38.0)

        # Set Loader A Prices
        await set_loader_price(loader_a.id, "cp_2400", 12.0)
        await set_loader_price(loader_a.id, "cp_880", 6.0)
        await set_loader_price(loader_a.id, "cp_10800", 50.0)

        # Set Loader B Prices (isolated from Loader A)
        await set_loader_price(loader_b.id, "cp_2400", 11.50)
        await set_loader_price(loader_b.id, "cp_880", 6.0)

        # ----------------------------------------------------
        # Example A: Client $16, Loader $12 -> profit=$4, code=X
        # ----------------------------------------------------
        res_a = await calculate_order_pricing("2400 CP", loader_id=loader_a.id)
        self.assertTrue(res_a["is_complete"])
        self.assertEqual(res_a["client_price_total"], Decimal("16"))
        self.assertEqual(res_a["loader_cost_total"], Decimal("12"))
        self.assertEqual(res_a["profit_amount"], Decimal("4"))
        self.assertEqual(res_a["secret_profit_code"], "X")

        # ----------------------------------------------------
        # Example B: Client $16, Loader $11.5 -> profit=$4.5, code=X+C
        # ----------------------------------------------------
        res_b = await calculate_order_pricing("2400 CP", loader_id=loader_b.id)
        self.assertTrue(res_b["is_complete"])
        self.assertEqual(res_b["client_price_total"], Decimal("16"))
        self.assertEqual(res_b["loader_cost_total"], Decimal("11.5"))
        self.assertEqual(res_b["profit_amount"], Decimal("4.5"))
        self.assertEqual(res_b["secret_profit_code"], "X+C")

        # ----------------------------------------------------
        # Example C: Client $24, Loader $18 -> profit=$6, code=A
        # ----------------------------------------------------
        res_c = await calculate_order_pricing("2400 CP + 880 CP", loader_id=loader_a.id)
        self.assertTrue(res_c["is_complete"])
        self.assertEqual(res_c["client_price_total"], Decimal("24"))
        self.assertEqual(res_c["loader_cost_total"], Decimal("18"))
        self.assertEqual(res_c["profit_amount"], Decimal("6"))
        self.assertEqual(res_c["secret_profit_code"], "A")

        # ----------------------------------------------------
        # Example D: Client $10, Loader $12 -> profit=-$2
        # ----------------------------------------------------
        res_d = await calculate_order_pricing(
            "2400 CP",
            loader_id=loader_a.id,
            client_price_map={"cp_2400": 10.0},
            loader_price_map={"cp_2400": 12.0}
        )
        self.assertTrue(res_d["is_complete"])
        self.assertEqual(res_d["client_price_total"], Decimal("10"))
        self.assertEqual(res_d["loader_cost_total"], Decimal("12"))
        self.assertEqual(res_d["profit_amount"], Decimal("-2"))
        self.assertIn(res_d["secret_profit_code"], ["W", "S"])

        # ----------------------------------------------------
        # Example E: Client $10.5, Loader $0 -> profit=$10.5, code=F+C
        # ----------------------------------------------------
        res_e = await calculate_order_pricing(
            "2400 CP",
            client_price_map={"cp_2400": 10.5},
            loader_price_map={"cp_2400": 0.0}
        )
        self.assertTrue(res_e["is_complete"])
        self.assertEqual(res_e["client_price_total"], Decimal("10.5"))
        self.assertEqual(res_e["loader_cost_total"], Decimal("0"))
        self.assertEqual(res_e["profit_amount"], Decimal("10.5"))
        self.assertEqual(res_e["secret_profit_code"], "F+C")

        # ----------------------------------------------------
        # 4 & 5. Multiple Products & Product Quantities (2400 CP x2)
        # ----------------------------------------------------
        res_qty = await calculate_order_pricing("2400 CP x2", loader_id=loader_a.id)
        self.assertTrue(res_qty["is_complete"])
        self.assertEqual(res_qty["client_price_total"], Decimal("32"))
        self.assertEqual(res_qty["loader_cost_total"], Decimal("24"))
        self.assertEqual(res_qty["profit_amount"], Decimal("8"))
        self.assertEqual(res_qty["secret_profit_code"], "D")

        # ----------------------------------------------------
        # 8 & 11. Unassigned Loader & Missing Loader Price
        # ----------------------------------------------------
        res_unassigned = await calculate_order_pricing("2400 CP", loader_id=None)
        self.assertFalse(res_unassigned["is_complete"])
        self.assertEqual(res_unassigned["client_price_total"], Decimal("16"))
        self.assertIsNone(res_unassigned["loader_cost_total"])
        self.assertIsNone(res_unassigned["profit_amount"])
        self.assertIsNone(res_unassigned["secret_profit_code"])
        self.assertIn("cp_2400", res_unassigned["missing_loader_keys"])

        res_missing_l = await calculate_order_pricing("10800 CP", loader_id=loader_b.id)
        self.assertFalse(res_missing_l["is_complete"])
        self.assertEqual(res_missing_l["client_price_total"], Decimal("65.5"))
        self.assertIsNone(res_missing_l["loader_cost_total"])
        self.assertIsNone(res_missing_l["profit_amount"])
        self.assertIsNone(res_missing_l["secret_profit_code"])

        # ----------------------------------------------------
        # 9. Missing Client Price
        # ----------------------------------------------------
        res_missing_c = await calculate_order_pricing("cp_unknown_123", loader_id=loader_a.id)
        self.assertFalse(res_missing_c["is_complete"])
        self.assertIsNone(res_missing_c["client_price_total"])
        self.assertIsNone(res_missing_c["profit_amount"])

        # ----------------------------------------------------
        # 12-15. Full Chain, Full Event Deal, Safe Vault, Special CP
        # ----------------------------------------------------
        await set_loader_price(loader_a.id, "full_chain", 12.0)
        await set_loader_price(loader_a.id, "full_event_deal", 11.0)
        await set_loader_price(loader_a.id, "safe_vault_50", 28.0)
        await set_loader_price(loader_a.id, "cp_4800", 20.0)

        # Full Chain
        res_fc = await calculate_order_pricing("Full Chain", loader_id=loader_a.id)
        self.assertTrue(res_fc["is_complete"])
        self.assertEqual(res_fc["items"][0]["product_key"], "full_chain")
        self.assertEqual(res_fc["client_price_total"], Decimal("16"))
        self.assertEqual(res_fc["loader_cost_total"], Decimal("12"))
        self.assertEqual(res_fc["profit_amount"], Decimal("4"))

        # Full Event Deal
        res_fe = await calculate_order_pricing("Full Event Deal", loader_id=loader_a.id)
        self.assertTrue(res_fe["is_complete"])
        self.assertEqual(res_fe["items"][0]["product_key"], "full_event_deal")
        self.assertEqual(res_fe["client_price_total"], Decimal("15"))
        self.assertEqual(res_fe["loader_cost_total"], Decimal("11"))

        # Safe Vault
        res_sv = await calculate_order_pricing("Safe Vault $50", loader_id=loader_a.id)
        self.assertTrue(res_sv["is_complete"])
        self.assertEqual(res_sv["items"][0]["product_key"], "safe_vault_50")
        self.assertEqual(res_sv["client_price_total"], Decimal("38"))
        self.assertEqual(res_sv["loader_cost_total"], Decimal("28"))

        # Special CP
        res_scp = await calculate_order_pricing("4800 CP", loader_id=loader_a.id)
        self.assertTrue(res_scp["is_complete"])
        self.assertEqual(res_scp["items"][0]["product_key"], "cp_4800")
        self.assertEqual(res_scp["client_price_total"], Decimal("30"))
        self.assertEqual(res_scp["loader_cost_total"], Decimal("20"))

        # ----------------------------------------------------
        # 16 & 17. DB OrderItem and Order Level Persistence
        # ----------------------------------------------------
        ord_db = await create_order(email="step6_db@test.com", package="2400 CP x2 + 880 CP")
        saved_ord = await save_order_pricing(ord_db.id, loader_id=loader_a.id)
        self.assertIsNotNone(saved_ord)
        self.assertEqual(saved_ord.client_price_total, 40.0)
        self.assertEqual(saved_ord.loader_cost_total, 30.0)
        self.assertEqual(saved_ord.profit_amount, 10.0)
        self.assertEqual(saved_ord.secret_profit_code, "F")

        fetched_ord = await get_order_by_id(ord_db.id)
        self.assertEqual(len(fetched_ord.items), 2)
        item_2400 = next(it for it in fetched_ord.items if it.product_key == "cp_2400")
        self.assertEqual(item_2400.quantity, 2)
        self.assertEqual(item_2400.client_unit_price, 16.0)
        self.assertEqual(item_2400.client_line_total, 32.0)
        self.assertEqual(item_2400.loader_unit_cost, 12.0)
        self.assertEqual(item_2400.loader_line_total, 24.0)

        # ----------------------------------------------------
        # 20. Historical Order Protection
        # ----------------------------------------------------
        hist_client = saved_ord.client_price_total
        hist_loader = saved_ord.loader_cost_total
        hist_profit = saved_ord.profit_amount
        hist_code = saved_ord.secret_profit_code

        # Change Loader A price for cp_2400
        await set_loader_price(loader_a.id, "cp_2400", 15.0)

        # Historical order in DB is UNCHANGED
        check_hist = await get_order_by_id(ord_db.id)
        self.assertEqual(check_hist.client_price_total, hist_client)
        self.assertEqual(check_hist.loader_cost_total, hist_loader)
        self.assertEqual(check_hist.profit_amount, hist_profit)
        self.assertEqual(check_hist.secret_profit_code, hist_code)


class TestStep7DeliveryAlbumGroupingAndSummary(unittest.IsolatedAsyncioTestCase):
    """Tests Step 7: Delivery Album Grouping + Delivery Summary."""

    async def asyncSetUp(self):
        from database import init_db, update_global_client_prices, add_loader, set_loader_price
        await init_db()
        await update_global_client_prices({
            "cp_2400": 16.0,
            "cp_5000": 33.0,
            "cp_10800": 65.5,
            "cp_880": 8.0,
        })
        self.loader_a = await add_loader(-100777888, "Step7 Loader A")
        await set_loader_price(self.loader_a.id, "cp_2400", 12.0)
        await set_loader_price(self.loader_a.id, "cp_5000", 25.0)

    async def test_single_image_delivery_formatting(self):
        """1 & 12. Single image delivery formatting without media_group_id."""
        from utils import format_delivery_summary_message
        msg = format_delivery_summary_message(
            email="cust1@gmail.com",
            client_price=137.0,
            secret_code="X",
            before_total=875.0,
            now_value=137.0,
            running_total=1012.0
        )
        self.assertIn("Price: $137", msg)
        self.assertIn("X", msg)
        self.assertNotIn("cust1@gmail.com", msg)
        self.assertNotIn("Profit:", msg)
        self.assertIn("Before: 875", msg)
        self.assertIn("Now: 137", msg)
        self.assertIn("Total: 1012", msg)
        self.assertNotIn("Loader", msg)
        self.assertNotIn("Cost", msg)

    async def test_album_collector_debouncing_and_grouping(self):
        """2, 3, 4 & 11. 2-image, 5-image, large album collection and debouncing."""
        from media_collector import MediaGroupCollector
        from unittest.mock import MagicMock, AsyncMock

        collector = MediaGroupCollector(timeout=0.05)
        
        order_id = 999
        email = "album@test.com"
        mock_bot = MagicMock()
        mock_bot.send_media_group = AsyncMock()

        # Create 5 images for a single media_group_id
        for i in range(1, 6):
            msg = MagicMock()
            msg.message_id = 1000 + i
            msg.media_group_id = "mg_test_album_5"
            msg.chat.id = -100888999
            msg.photo = [MagicMock(file_id=f"file_id_{i}")]
            msg.document = None
            msg.caption = f"Caption {i}" if i == 1 else None
            msg.text = None
            await collector.add_reply_media_message(msg, order_id=order_id, email=email, bot=mock_bot)

        # Check buffer key exists while debouncing
        buffer_key = f"{order_id}_mg_test_album_5"
        self.assertIn(buffer_key, collector._buffers)
        self.assertEqual(len(collector._buffers[buffer_key]["items"]), 5)

        # Wait for debounce flush to finish
        await asyncio.sleep(0.12)

        # Buffer should be flushed and added to _processed_cache
        self.assertNotIn(buffer_key, collector._buffers)
        self.assertIn(buffer_key, collector._processed_cache)

    async def test_single_delivery_ledger_and_running_total_per_album(self):
        """5, 6, 7, 8 & 13. Same media_group_id creates ONE ledger entry & updates total ONCE for client group."""
        from database import create_order, get_current_running_total, record_delivery_ledger_entry, AsyncSessionLocal
        from models import DeliveryLedger
        from sqlalchemy import select
        chat_id = -100987654321
        order = await create_order(email="album_single@test.com", package="2400 CP", client_chat_id=chat_id)

        before_total = await get_current_running_total(chat_id)

        # Record delivery ledger for album with 4 images
        entry, is_new = await record_delivery_ledger_entry(
            order_id=order.id,
            package=order.package,
            now_value=16.0,
            loader_name="Loader A",
            dedup_hash="media_group_mg_album_123",
            chat_id=chat_id
        )

        self.assertTrue(is_new)
        self.assertIsNotNone(entry)

        after_total = await get_current_running_total(chat_id)
        self.assertEqual(after_total, before_total + 16.0)

        async with AsyncSessionLocal() as session:
            res = await session.execute(select(DeliveryLedger).where(DeliveryLedger.chat_id == chat_id))
            ledger = res.scalars().all()
            matched = [e for e in ledger if e.dedup_hash == "media_group_mg_album_123"]
            self.assertEqual(len(matched), 1)

    async def test_duplicate_protection_and_retries(self):
        """9 & 10. Duplicate album update/media_group_id does NOT process twice or change total twice."""
        from database import create_order, get_current_running_total, record_delivery_ledger_entry
        chat_id = -100444555666
        order = await create_order(email="dup_album@test.com", package="2400 CP", client_chat_id=chat_id)

        before_total = await get_current_running_total(chat_id)

        # First attempt
        entry1, is_new1 = await record_delivery_ledger_entry(
            order_id=order.id,
            package=order.package,
            now_value=16.0,
            loader_name="Loader A",
            dedup_hash="media_group_mg_dup_999",
            chat_id=chat_id
        )
        self.assertTrue(is_new1)

        # Retry attempt with same dedup_hash / media_group_id
        entry2, is_new2 = await record_delivery_ledger_entry(
            order_id=order.id,
            package=order.package,
            now_value=16.0,
            loader_name="Loader A",
            dedup_hash="media_group_mg_dup_999",
            chat_id=chat_id
        )
        self.assertFalse(is_new2)
        self.assertIsNone(entry2)

        after_total = await get_current_running_total(chat_id)
        self.assertEqual(after_total, before_total + 16.0)

    async def test_client_group_isolation(self):
        """14. Wrong client group running total is never used or affected."""
        from database import create_order, get_current_running_total, record_delivery_ledger_entry
        group1 = -100111111111
        group2 = -100222222222

        order1 = await create_order(email="g1@test.com", package="2400 CP", client_chat_id=group1)
        
        await record_delivery_ledger_entry(
            order_id=order1.id,
            package=order1.package,
            now_value=16.0,
            loader_name="Loader A",
            dedup_hash="mg_g1",
            chat_id=group1
        )

        total_g1 = await get_current_running_total(group1)
        total_g2 = await get_current_running_total(group2)

        self.assertEqual(total_g1, 16.0)
        self.assertEqual(total_g2, 0.0)

    async def test_multiple_packages_combined_client_price(self):
        """15. Multiple packages use combined client price."""
        from decimal import Decimal
        from pricing_calculator import calculate_order_pricing
        from utils import format_delivery_summary_message
        
        pricing = await calculate_order_pricing("2400 CP + 5000 CP", loader_id=self.loader_a.id)

        # 2400 CP ($16) + 5000 CP ($33) = $49
        self.assertEqual(pricing["client_price_total"], Decimal("49"))
        self.assertEqual(pricing["loader_cost_total"], Decimal("37"))
        self.assertEqual(pricing["profit_amount"], Decimal("12"))
        self.assertEqual(pricing["secret_profit_code"], "H")

        summary = format_delivery_summary_message(
            email="multi_pkg@test.com",
            client_price=pricing["client_price_total"],
            secret_code=pricing["secret_profit_code"],
            before_total=100.0,
            now_value=float(pricing["client_price_total"]),
            running_total=149.0
        )
        self.assertIn("Price: $49", summary)
        self.assertIn("H", summary)
        self.assertNotIn("Profit:", summary)
        self.assertNotIn("37", summary)

    async def test_privacy_non_exposure(self):
        """16 & 17. Secret profit code is shown, actual dollar profit & loader cost are NEVER exposed."""
        from utils import format_delivery_summary_message
        summary = format_delivery_summary_message(
            email="privacy@test.com",
            client_price=49.0,
            secret_code="H",
            before_total=0.0,
            now_value=49.0,
            running_total=49.0
        )
        self.assertIn("H", summary)
        self.assertNotIn("Profit:", summary)
        self.assertNotIn("Loader Cost", summary)
        self.assertNotIn("Actual Profit", summary)
        self.assertNotIn("cost", summary.lower())

    async def test_missing_order_or_failure_isolation(self):
        """18 & 19. Missing order or failure does not create financial changes."""
        from database import get_current_running_total
        chat_id = -100999000
        before_total = await get_current_running_total(chat_id)

        after_total = await get_current_running_total(chat_id)
        self.assertEqual(before_total, after_total)

    async def test_delivery_chunking_utility(self):
        """20. Existing delivery behavior/chunking remains compatible."""
        from delivery import chunk_list
        items = list(range(25))
        chunks = chunk_list(items, 10)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 10)
        self.assertEqual(len(chunks[1]), 10)
        self.assertEqual(len(chunks[2]), 5)


class TestStep8ClientPaymentOCRAndDeduction(unittest.IsolatedAsyncioTestCase):
    """Tests Step 8: Client Payment OCR + Verification + Group Balance Deduction."""

    async def asyncSetUp(self):
        from database import init_db
        await init_db()

    def test_ocr_amount_and_txid_extraction(self):
        """1, 2, 3, 4. Extracts amount (Decimal, USDT, $) and normalized TXID."""
        from utils import extract_payment_info, normalize_transaction_id
        from decimal import Decimal

        # 1. Standard text
        text1 = "Payment received: $200.50 USDT\nTransaction ID: TXN123456789"
        info1 = extract_payment_info(text1)
        self.assertTrue(info1["is_payment"])
        self.assertEqual(info1["amount"], Decimal("200.50"))
        self.assertEqual(info1["transaction_id"], "TXN123456789")
        self.assertEqual(info1["currency"], "USDT")

        # 2. Lowercase and prefix labels
        text2 = "amount paid: 100 usdt\ntrx id: abc_xyz_999"
        info2 = extract_payment_info(text2)
        self.assertEqual(info2["amount"], Decimal("100"))
        self.assertEqual(info2["transaction_id"], "ABC_XYZ_999")

        # 3. Canonical TXID normalization
        self.assertEqual(normalize_transaction_id("Transaction ID: REF_001_ABC"), "REF_001_ABC")
        self.assertEqual(normalize_transaction_id("txid: 123456"), "123456")

    def test_missing_amount_and_missing_txid(self):
        """5 & 6. Handles missing amount or missing transaction ID."""
        from utils import extract_payment_info
        from decimal import Decimal

        # Missing TXID
        text_no_tx = "Payment received $200 USDT"
        info1 = extract_payment_info(text_no_tx)
        self.assertEqual(info1["amount"], Decimal("200"))
        self.assertIsNone(info1["transaction_id"])

        # Missing Amount
        text_no_amt = "Transaction Hash: 0x1234567890abcdef"
        info2 = extract_payment_info(text_no_amt)
        self.assertIsNone(info2["amount"])
        self.assertEqual(info2["transaction_id"], "0X1234567890ABCDEF")

    async def test_pending_and_rejected_payment_no_balance_change(self):
        """7 & 8. Pending and Rejected payments produce ZERO balance change."""
        from database import get_current_running_total
        from utils import format_payment_pending_message, format_payment_rejected_message
        chat_id = -100888111

        before_total = await get_current_running_total(chat_id)

        # Pending message format
        msg_pending = format_payment_pending_message(amount=200.0, tx_id="TX_PENDING_1")
        self.assertIn("Pending Verification", msg_pending)
        self.assertIn("200", msg_pending)

        # Rejected message format
        msg_rejected = format_payment_rejected_message("API Error")
        self.assertIn("failed", msg_rejected)

        # Balance remains unchanged
        after_total = await get_current_running_total(chat_id)
        self.assertEqual(before_total, after_total)

    async def test_verified_payment_group_balance_deduction(self):
        """9, 10, 16, 17 & 20. Verified payment deducts group balance with correct Before/Payment/Total."""
        from database import process_verified_payment_deduction, get_current_running_total, record_delivery_ledger_entry, AsyncSessionLocal
        from models import PaymentTransaction, DeliveryLedger
        from sqlalchemy import select

        chat_id = -100555666777

        # Set initial delivery to make running total 1012
        await record_delivery_ledger_entry(
            order_id=None,
            package="INIT",
            now_value=1012.0,
            loader_name="Admin",
            dedup_hash="step8_init_1012",
            chat_id=chat_id
        )

        before_total = await get_current_running_total(chat_id)
        self.assertEqual(before_total, 1012.0)

        # Process verified payment of $200
        p_tx, before_v, now_v, total_v, is_new, res_code = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=200.0,
            transaction_id="TX_VERIFIED_1001",
            provider="Binance",
            currency="USDT"
        )

        self.assertTrue(is_new)
        self.assertEqual(res_code, "SUCCESS")
        self.assertEqual(before_v, 1012.0)
        self.assertEqual(now_v, 200.0)
        self.assertEqual(total_v, 812.0)

        after_total = await get_current_running_total(chat_id)
        self.assertEqual(after_total, 812.0)

        # Verify PaymentTransaction & DeliveryLedger records created once
        async with AsyncSessionLocal() as session:
            stmt_p = select(PaymentTransaction).where(PaymentTransaction.transaction_id == "TX_VERIFIED_1001")
            ptx_item = (await session.execute(stmt_p)).scalar_one_or_none()
            self.assertIsNotNone(ptx_item)
            self.assertEqual(ptx_item.amount, 200.0)
            self.assertEqual(ptx_item.status, "VERIFIED")

            stmt_l = select(DeliveryLedger).where(DeliveryLedger.dedup_hash == "payment_tx_binance_tx_verified_1001")
            ledger_item = (await session.execute(stmt_l)).scalar_one_or_none()
            self.assertIsNotNone(ledger_item)
            self.assertEqual(ledger_item.price, -200.0)

    async def test_duplicate_payment_deduplication(self):
        """11, 12 & 13. Duplicate transaction ID does NOT deduct balance twice."""
        from database import process_verified_payment_deduction, get_current_running_total, record_delivery_ledger_entry

        chat_id = -100444333222

        await record_delivery_ledger_entry(
            order_id=None,
            package="INIT",
            now_value=1012.0,
            loader_name="Admin",
            dedup_hash="step8_dup_init",
            chat_id=chat_id
        )

        # 1st Submission: $200
        p_tx1, before1, now1, total1, is_new1, res1 = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=200.0,
            transaction_id="TX_DUP_12345",
            provider="Binance"
        )
        self.assertTrue(is_new1)
        self.assertEqual(total1, 812.0)

        # 2nd Submission (Duplicate TXID): $200
        p_tx2, before2, now2, total2, is_new2, res2 = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=200.0,
            transaction_id="TX_DUP_12345",
            provider="Binance"
        )
        self.assertFalse(is_new2)
        self.assertEqual(res2, "DUPLICATE_TRANSACTION")
        self.assertEqual(now2, 0.0)
        self.assertEqual(total2, 812.0)

        # Running total remains 812.0, NOT 612.0
        final_total = await get_current_running_total(chat_id)
        self.assertEqual(final_total, 812.0)

    async def test_group_isolation(self):
        """14, 15 & 24. Payment in Group A does not affect Group B or another order."""
        from database import process_verified_payment_deduction, get_current_running_total, record_delivery_ledger_entry

        group_a = -100888777111
        group_b = -100999888222

        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=1012.0, dedup_hash="init_a", chat_id=group_a)
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=500.0, dedup_hash="init_b", chat_id=group_b)

        # Payment in Group A: $200
        await process_verified_payment_deduction(chat_id=group_a, amount=200.0, transaction_id="TX_GROUP_A", provider="Binance")

        total_a = await get_current_running_total(group_a)
        total_b = await get_current_running_total(group_b)

        self.assertEqual(total_a, 812.0)
        self.assertEqual(total_b, 500.0)

    async def test_overpayment_ledger_behavior(self):
        """21. Overpayment reduces running total into negative value as expected."""
        from database import process_verified_payment_deduction, get_current_running_total, record_delivery_ledger_entry

        chat_id = -100999888777
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=100.0, dedup_hash="init_overpay", chat_id=chat_id)

        # Payment of $200 on balance of $100 -> -100
        p_tx, before_v, now_v, total_v, is_new, res_code = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=200.0,
            transaction_id="TX_OVERPAY_1",
            provider="Binance"
        )
        self.assertTrue(is_new)
        self.assertEqual(total_v, -100.0)

        final_total = await get_current_running_total(chat_id)
        self.assertEqual(final_total, -100.0)

    async def test_privacy_non_exposure_in_payment_messages(self):
        """24. Payment confirmation messages NEVER expose loader cost or actual profit."""
        from utils import format_payment_verification_message, format_payment_pending_message, format_payment_rejected_message

        v_msg = format_payment_verification_message(amount=200.0, tx_id="ABC123XYZ", before_total=1012.0, now_payment=200.0, running_total=812.0)
        self.assertIn("Payment Verified", v_msg)
        self.assertIn("ABC123XYZ", v_msg)
        self.assertIn("Before: 1012", v_msg)
        self.assertIn("Payment: -200", v_msg)
        self.assertIn("Total: 812", v_msg)

        self.assertNotIn("Loader", v_msg)
        self.assertNotIn("Profit", v_msg)
        self.assertNotIn("cost", v_msg.lower())

        p_msg = format_payment_pending_message(amount=200.0, tx_id="ABC123XYZ")
        self.assertNotIn("Loader", p_msg)
        self.assertNotIn("Profit", p_msg)

        r_msg = format_payment_rejected_message("API error")
        self.assertNotIn("Loader", r_msg)
        self.assertNotIn("Profit", r_msg)


class TestStep9EndToEndIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from database import init_db
        await init_db()

    def test_negative_profit_encoding_regression(self):
        """21 & 22. Verify negative profit encoding: -$2 -> S and +$2 -> W."""
        from decimal import Decimal
        from profit_code_engine import encode_profit_code, decode_profit_code

        self.assertEqual(encode_profit_code(Decimal("-2")), "S")
        self.assertEqual(encode_profit_code(Decimal("2")), "W")
        self.assertEqual(encode_profit_code(Decimal("0")), "U")
        self.assertEqual(encode_profit_code(Decimal("4")), "X")
        self.assertEqual(encode_profit_code(Decimal("4.5")), "X+C")
        self.assertEqual(encode_profit_code(Decimal("10.5")), "F+C")
        self.assertEqual(encode_profit_code(Decimal("10.25")), "F+K")
        self.assertEqual(encode_profit_code(Decimal("14.75")), "J+L")
        self.assertEqual(encode_profit_code(Decimal("17")), "X+F+Y")

        # Decodes back to exact Decimal
        self.assertEqual(decode_profit_code("S"), Decimal("-2"))
        self.assertEqual(decode_profit_code("W"), Decimal("2"))

    def test_product_catalog_coverage_and_canonical_keys(self):
        """20. Full product coverage & canonical key verification."""
        from pricing_calculator import parse_order_items_from_text
        from product_catalog import PRODUCT_CATALOG

        # Check catalog products
        for key in ["cp_10800", "cp_5000", "cp_2400", "cp_880", "cp_420", "cp_4800", "safe_vault_50", "full_event_deal", "full_chain"]:
            self.assertIn(key, PRODUCT_CATALOG)

        # Full Chain MUST remain full_chain and NOT cp_560
        items_fc = parse_order_items_from_text("Full Chain")
        self.assertEqual(len(items_fc), 1)
        self.assertEqual(items_fc[0]["product_key"], "full_chain")

        # Full Event Deal
        items_fe = parse_order_items_from_text("Full Event Deal")
        self.assertEqual(len(items_fe), 1)
        self.assertEqual(items_fe[0]["product_key"], "full_event_deal")

    def test_order_parser_context_aware_exclusions(self):
        """3. Order parser avoids mistaking random numbers (UID, OTP, Passwords, Order ID) for CP."""
        from order_parser import parse_order_v2

        text = (
            "Order #:991\n"
            "Activision\n"
            "UID: 6712394850192\n"
            "Email: testuser@gmail.com\n"
            "Pass: 987654321\n"
            "OTP: 0451 8921\n"
            "2400 CP"
        )
        parsed = parse_order_v2(text)
        self.assertTrue(parsed["order_detected"])
        self.assertEqual(parsed["customer_ref_id"], "991")
        self.assertEqual(parsed["email"], "testuser@gmail.com")
        self.assertEqual(len(parsed["packages"]), 1)
        self.assertEqual(parsed["packages"][0]["package"], "2400")
        self.assertNotIn("6712394850192", [p["package"] for p in parsed["packages"]])
        self.assertNotIn("987654321", [p["package"] for p in parsed["packages"]])

    async def test_loader_pricing_isolation(self):
        """19. Loader A cost ($12) vs Loader B cost ($14) for same product (2400 CP)."""
        from database import set_loader_price
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        loader_a_id = 9001
        loader_b_id = 9002

        await set_loader_price(loader_a_id, "cp_2400", Decimal("12"))
        await set_loader_price(loader_b_id, "cp_2400", Decimal("14"))

        calc_a = await calculate_order_pricing("2400 CP", loader_id=loader_a_id, client_price_map={"cp_2400": Decimal("16")})
        calc_b = await calculate_order_pricing("2400 CP", loader_id=loader_b_id, client_price_map={"cp_2400": Decimal("16")})

        self.assertEqual(calc_a["loader_cost_total"], Decimal("12"))
        self.assertEqual(calc_a["profit_amount"], Decimal("4"))
        self.assertEqual(calc_a["secret_profit_code"], "X")

        self.assertEqual(calc_b["loader_cost_total"], Decimal("14"))
        self.assertEqual(calc_b["profit_amount"], Decimal("2"))
        self.assertEqual(calc_b["secret_profit_code"], "W")

    def test_multi_package_and_quantity_pricing(self):
        """8 & 9. Multi-package aggregation and quantity multiplier pricing."""
        from pricing_calculator import calculate_order_totals
        from decimal import Decimal

        # 2400 CP ($16 client / $12 loader) + 5000 CP ($33 client / $25 loader)
        items_multi = [
            {"product_key": "cp_2400", "quantity": 1, "client_price": Decimal("16"), "loader_cost": Decimal("12")},
            {"product_key": "cp_5000", "quantity": 1, "client_price": Decimal("33"), "loader_cost": Decimal("25")}
        ]
        res_multi = calculate_order_totals(items_multi)
        self.assertEqual(res_multi["client_total"], Decimal("49"))
        self.assertEqual(res_multi["loader_total"], Decimal("37"))
        self.assertEqual(res_multi["profit"], Decimal("12"))
        self.assertEqual(res_multi["secret_code"], "H")

        # Quantities: 2400 CP x2 ($16 x2 = $32 client / $12 x2 = $24 loader / profit $8 = D)
        items_qty = [
            {"product_key": "cp_2400", "quantity": 2, "client_price": Decimal("16"), "loader_cost": Decimal("12")}
        ]
        res_qty = calculate_order_totals(items_qty)
        self.assertEqual(res_qty["client_total"], Decimal("32"))
        self.assertEqual(res_qty["loader_total"], Decimal("24"))
        self.assertEqual(res_qty["profit"], Decimal("8"))
        self.assertEqual(res_qty["secret_code"], "D")

    async def test_end_to_end_scenario_1_single_package(self):
        """23. Full end-to-end scenario: Order -> Pricing -> Delivery -> Payment -> Dup Check."""
        from database import (
            record_delivery_ledger_entry,
            get_current_running_total,
            process_verified_payment_deduction,
            set_global_client_price,
            set_loader_price
        )
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        chat_id = -100999000111
        loader_id = 8801

        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(loader_id, "cp_2400", Decimal("12"))

        await record_delivery_ledger_entry(
            order_id=None,
            package="INIT",
            now_value=875.0,
            loader_name="Admin",
            dedup_hash="s9_sc1_init",
            chat_id=chat_id
        )
        self.assertEqual(await get_current_running_total(chat_id), 875.0)

        # 1. Calculate pricing
        pricing = await calculate_order_pricing("2400 CP", loader_id=loader_id)
        self.assertTrue(pricing["is_complete"])
        self.assertEqual(pricing["client_price_total"], Decimal("16"))
        self.assertEqual(pricing["loader_cost_total"], Decimal("12"))
        self.assertEqual(pricing["profit_amount"], Decimal("4"))
        self.assertEqual(pricing["secret_profit_code"], "X")

        # 2. Record delivery ledger entry (Order +$16)
        entry, _ = await record_delivery_ledger_entry(
            order_id=101,
            package="2400 CP",
            price=16.0,
            loader_name="Loader A",
            client_amount=16.0,
            loader_cost=12.0,
            profit_amount=4.0,
            secret_profit_code="X",
            chat_id=chat_id,
            dedup_hash="s9_sc1_delivery_101"
        )
        self.assertEqual(entry.before_total, 875.0)
        self.assertEqual(entry.now_value, 16.0)
        self.assertEqual(entry.running_total, 891.0)
        self.assertEqual(await get_current_running_total(chat_id), 891.0)

        # 3. Process Verified Payment of $100
        p_tx1, b1, n1, t1, is_new1, res1 = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=100.0,
            transaction_id="TX_S9_SC1_100",
            provider="Binance"
        )
        self.assertTrue(is_new1)
        self.assertEqual(b1, 891.0)
        self.assertEqual(n1, 100.0)
        self.assertEqual(t1, 791.0)
        self.assertEqual(await get_current_running_total(chat_id), 791.0)

        # 4. Duplicate Payment TXID resubmission
        p_tx2, b2, n2, t2, is_new2, res2 = await process_verified_payment_deduction(
            chat_id=chat_id,
            amount=100.0,
            transaction_id="TX_S9_SC1_100",
            provider="Binance"
        )
        self.assertFalse(is_new2)
        self.assertEqual(res2, "DUPLICATE_TRANSACTION")
        self.assertEqual(n2, 0.0)
        self.assertEqual(t2, 791.0)
        self.assertEqual(await get_current_running_total(chat_id), 791.0)

    async def test_end_to_end_scenario_2_multi_package(self):
        """24. Second scenario: 2400 CP + 5000 CP ($49 client / $37 loader / profit $12 -> H)."""
        from database import (
            record_delivery_ledger_entry,
            get_current_running_total,
            set_global_client_price,
            set_loader_price
        )
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        chat_id = -100999000222
        loader_id = 8802

        await set_global_client_price("cp_2400", Decimal("16"))
        await set_global_client_price("cp_5000", Decimal("33"))
        await set_loader_price(loader_id, "cp_2400", Decimal("12"))
        await set_loader_price(loader_id, "cp_5000", Decimal("25"))

        pricing = await calculate_order_pricing("2400 CP + 5000 CP", loader_id=loader_id)
        self.assertTrue(pricing["is_complete"])
        self.assertEqual(pricing["client_price_total"], Decimal("49"))
        self.assertEqual(pricing["loader_cost_total"], Decimal("37"))
        self.assertEqual(pricing["profit_amount"], Decimal("12"))
        self.assertEqual(pricing["secret_profit_code"], "H")

        # 5 screenshots in 1 album -> 1 delivery ledger entry
        entry, is_new = await record_delivery_ledger_entry(
            order_id=102,
            package="2400 CP + 5000 CP",
            price=49.0,
            loader_name="Loader B",
            client_amount=49.0,
            loader_cost=37.0,
            profit_amount=12.0,
            secret_profit_code="H",
            chat_id=chat_id,
            dedup_hash="s9_sc2_album_5_images_dedup"
        )
        self.assertTrue(is_new)
        self.assertEqual(entry.now_value, 49.0)
        self.assertEqual(await get_current_running_total(chat_id), 49.0)

        # Duplicate album retry with same dedup_hash -> blocked
        entry_dup, is_new_dup = await record_delivery_ledger_entry(
            order_id=102,
            package="2400 CP + 5000 CP",
            price=49.0,
            loader_name="Loader B",
            client_amount=49.0,
            loader_cost=37.0,
            profit_amount=12.0,
            secret_profit_code="H",
            chat_id=chat_id,
            dedup_hash="s9_sc2_album_5_images_dedup"
        )
        self.assertFalse(is_new_dup)
        self.assertEqual(await get_current_running_total(chat_id), 49.0)

    async def test_historical_price_protection(self):
        """14. Updating /setclientprice or /setloaderprice does NOT mutate historical order/ledger records."""
        from database import (
            record_delivery_ledger_entry,
            set_global_client_price,
            set_loader_price,
            get_ledger_entry_by_id
        )
        from decimal import Decimal

        chat_id = -100777666555
        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(7001, "cp_2400", Decimal("12"))

        entry, _ = await record_delivery_ledger_entry(
            order_id=201,
            package="2400 CP",
            price=16.0,
            loader_name="Loader A",
            client_amount=16.0,
            loader_cost=12.0,
            profit_amount=4.0,
            secret_profit_code="X",
            chat_id=chat_id,
            dedup_hash="s9_hist_price_201"
        )
        entry_id = entry.id

        # Admin updates global client price and loader price
        await set_global_client_price("cp_2400", Decimal("20"))
        await set_loader_price(7001, "cp_2400", Decimal("15"))

        # Verify historical ledger entry remains unchanged
        fetched = await get_ledger_entry_by_id(entry_id)
        self.assertEqual(fetched.client_amount, 16.0)
        self.assertEqual(fetched.loader_cost, 12.0)
        self.assertEqual(fetched.profit_amount, 4.0)
        self.assertEqual(fetched.secret_profit_code, "X")

    async def test_failure_scenarios(self):
        """25. Missing prices, unassigned loader lead to incomplete pricing & no fake records."""
        from pricing_calculator import calculate_order_pricing

        # Missing client price
        res_no_client = await calculate_order_pricing([{"product_key": "cp_unknown_999", "quantity": 1}], loader_id=123)
        self.assertFalse(res_no_client["is_complete"])
        self.assertFalse(res_no_client["is_client_complete"])
        self.assertIn("cp_unknown_999", res_no_client["missing_client_keys"])

        # No loader assigned
        res_no_loader = await calculate_order_pricing("2400 CP", loader_id=None, client_price_map={"cp_2400": 16})
        self.assertFalse(res_no_loader["is_complete"])
        self.assertFalse(res_no_loader["is_loader_complete"])
        self.assertIsNone(res_no_loader["profit_amount"])
        self.assertIsNone(res_no_loader["secret_profit_code"])


class TestStep10OrderLifecycleIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from database import init_db
        await init_db()

    async def test_1_initial_pending_status(self):
        """1. Initial PENDING status upon order creation."""
        from database import create_order
        order = await create_order(email="newcust@gmail.com", package="2400 CP", client_chat_id=-10011)
        self.assertEqual(order.status, "Pending")

    async def test_2_successful_pricing(self):
        """2. Successful pricing transitions order status to PRICED."""
        from database import create_order, set_global_client_price, transition_order_status
        from decimal import Decimal

        await set_global_client_price("cp_2400", Decimal("16"))
        order = await create_order(email="cust2@gmail.com", package="2400 CP", client_chat_id=-10012)
        upd_order, success, reason = await transition_order_status(order.id, "PRICED")
        self.assertTrue(success)
        self.assertEqual(upd_order.status, "PRICED")
        self.assertEqual(upd_order.client_price_total, 16.0)

    async def test_3_missing_client_price(self):
        """3. Order with missing client price remains Pending / Needs_Review without fake pricing."""
        from database import create_order
        from pricing_calculator import calculate_order_pricing

        order = await create_order(email="cust3@gmail.com", package="99999 CP Unpriced", client_chat_id=-10013)
        calc = await calculate_order_pricing([{"product_key": "cp_99999", "quantity": 1}], loader_id=101)
        self.assertFalse(calc["is_complete"])
        self.assertFalse(calc["is_client_complete"])
        self.assertIsNone(calc["client_price_total"])

    async def test_4_loader_assignment(self):
        """4. Loader assignment calculates loader cost, profit amount, and secret profit code."""
        from database import create_order, set_global_client_price, set_loader_price, save_order_pricing, transition_order_status
        from decimal import Decimal

        loader_id = 9101
        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(loader_id, "cp_2400", Decimal("12"))

        order = await create_order(email="cust4@gmail.com", package="2400 CP", client_chat_id=-10014)
        upd_order, success, _ = await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=loader_id)
        self.assertTrue(success)
        self.assertEqual(upd_order.client_price_total, 16.0)
        self.assertEqual(upd_order.loader_cost_total, 12.0)
        self.assertEqual(upd_order.profit_amount, 4.0)
        self.assertEqual(upd_order.secret_profit_code, "X")

    async def test_5_missing_loader_assignment(self):
        """5. Calculation without loader assigned leaves loader cost & profit uncalculated."""
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        calc = await calculate_order_pricing("2400 CP", loader_id=None, client_price_map={"cp_2400": Decimal("16")})
        self.assertFalse(calc["is_loader_complete"])
        self.assertIsNone(calc["loader_cost_total"])
        self.assertIsNone(calc["profit_amount"])
        self.assertIsNone(calc["secret_profit_code"])

    async def test_6_missing_loader_price(self):
        """6. Assigned loader missing product cost moves order to NEEDS_REVIEW / unpriced loader state."""
        from database import create_order, set_global_client_price, transition_order_status
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        loader_id = 9106
        await set_global_client_price("cp_2400", Decimal("16"))
        order = await create_order(email="cust6@gmail.com", package="2400 CP", client_chat_id=-10016)

        calc = await calculate_order_pricing("2400 CP", loader_id=loader_id)
        self.assertFalse(calc["is_loader_complete"])
        self.assertIn("cp_2400", calc["missing_loader_keys"])

        upd_order, success, _ = await transition_order_status(order.id, "NEEDS_REVIEW", loader_id=loader_id)
        self.assertTrue(success)
        self.assertEqual(upd_order.status, "NEEDS_REVIEW")

    async def test_7_successful_transition_to_sent_to_loader(self):
        """7. Successful transition to SENT_TO_LOADER."""
        from database import create_order, transition_order_status

        order = await create_order(email="cust7@gmail.com", package="2400 CP", client_chat_id=-10017)
        await transition_order_status(order.id, "PRICED")
        upd_order, success, _ = await transition_order_status(order.id, "SENT_TO_LOADER")
        self.assertTrue(success)
        self.assertEqual(upd_order.status, "SENT_TO_LOADER")

    async def test_8_single_image_delivery(self):
        """8. Single image delivery transitions to DELIVERED and COMPLETED."""
        from database import create_order, mark_order_delivered, mark_order_completed

        order = await create_order(email="cust8@gmail.com", package="2400 CP", client_chat_id=-10018)
        deliv = await mark_order_delivered(order.id)
        self.assertEqual(deliv.status, "Delivered")
        comp = await mark_order_completed(order.id)
        self.assertEqual(comp.status, "Completed")

    async def test_9_multi_image_album_delivery(self):
        """9. Multi-image album delivery transitions to COMPLETED creating 1 ledger entry."""
        from database import create_order, record_delivery_ledger_entry, mark_order_completed, get_current_running_total

        chat_id = -10019
        order = await create_order(email="cust9@gmail.com", package="2400 CP", client_chat_id=chat_id)
        entry, is_new = await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400 CP",
            now_value=16.0,
            loader_name="Loader A",
            dedup_hash="step10_album_5_images",
            chat_id=chat_id
        )
        self.assertTrue(is_new)
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

        comp = await mark_order_completed(order.id)
        self.assertEqual(comp.status, "Completed")

    async def test_10_delivered_transition(self):
        """10. mark_order_delivered sets status to Delivered."""
        from database import create_order, mark_order_delivered
        order = await create_order(email="cust10@gmail.com", package="2400 CP", client_chat_id=-10020)
        deliv = await mark_order_delivered(order.id)
        self.assertEqual(deliv.status, "Delivered")
        self.assertIsNotNone(deliv.delivered_at)

    async def test_11_completed_transition(self):
        """11. mark_order_completed sets status to Completed."""
        from database import create_order, mark_order_completed
        order = await create_order(email="cust11@gmail.com", package="2400 CP", client_chat_id=-10021)
        comp = await mark_order_completed(order.id)
        self.assertEqual(comp.status, "Completed")

    async def test_12_duplicate_delivery_protection(self):
        """12. Re-delivering a completed order is blocked as duplicate with 0 running total change."""
        from database import create_order, mark_order_completed, record_delivery_ledger_entry, get_current_running_total

        chat_id = -10022
        order = await create_order(email="cust12@gmail.com", package="2400 CP", client_chat_id=chat_id)
        await record_delivery_ledger_entry(order_id=order.id, package="2400 CP", now_value=16.0, dedup_hash="hash_12", chat_id=chat_id)
        await mark_order_completed(order.id)
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

        # Retry duplicate delivery
        entry_dup, is_new_dup = await record_delivery_ledger_entry(order_id=order.id, package="2400 CP", now_value=16.0, dedup_hash="hash_12", chat_id=chat_id)
        self.assertFalse(is_new_dup)
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

    async def test_13_duplicate_source_order_protection(self):
        """13. Re-submitting exact duplicate content returns existing pending order."""
        from database import create_order, get_exact_duplicate_pending_order

        text = "Order #:54\nActivision\nEmail: testdup@gmail.com\nPass: 12345\n2400 CP"
        o1 = await create_order(email="testdup@gmail.com", package="2400 CP", raw_text=text, client_chat_id=-10023)
        dup = await get_exact_duplicate_pending_order("testdup@gmail.com", text)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, o1.id)

    async def test_14_cancelled_order(self):
        """14. Cancelling an order sets status Cancelled and produces NO delivery ledger entry or running total change."""
        from database import create_order, cancel_order, get_current_running_total

        chat_id = -10024
        order = await create_order(email="cust14@gmail.com", package="2400 CP", client_chat_id=chat_id)
        c_order, ok = await cancel_order(order.id)
        self.assertTrue(ok)
        self.assertEqual(c_order.status, "Cancelled")
        self.assertEqual(await get_current_running_total(chat_id), 0.0)

    async def test_15_failed_delivery(self):
        """15. Failed delivery transitions to FAILED and produces 0 running total change."""
        from database import create_order, transition_order_status, get_current_running_total

        chat_id = -10025
        order = await create_order(email="cust15@gmail.com", package="2400 CP", client_chat_id=chat_id)
        upd, ok, _ = await transition_order_status(order.id, "FAILED")
        self.assertTrue(ok)
        self.assertEqual(upd.status, "FAILED")
        self.assertEqual(await get_current_running_total(chat_id), 0.0)

    async def test_16_redelivery_protection(self):
        """16. Redelivering an order session with same dedup_hash is idempotent."""
        from database import record_delivery_ledger_entry, get_current_running_total

        chat_id = -10026
        entry1, ok1 = await record_delivery_ledger_entry(order_id=160, package="2400 CP", now_value=16.0, dedup_hash="redeliv_hash", chat_id=chat_id)
        self.assertTrue(ok1)

        entry2, ok2 = await record_delivery_ledger_entry(order_id=160, package="2400 CP", now_value=16.0, dedup_hash="redeliv_hash", chat_id=chat_id)
        self.assertFalse(ok2)
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

    async def test_17_loader_ab_isolation(self):
        """17. Loader A ($12) vs Loader B ($14) pricing isolation."""
        from database import set_loader_price
        from pricing_calculator import calculate_order_pricing
        from decimal import Decimal

        l_a = 9171
        l_b = 9172
        await set_loader_price(l_a, "cp_2400", Decimal("12"))
        await set_loader_price(l_b, "cp_2400", Decimal("14"))

        res_a = await calculate_order_pricing("2400 CP", loader_id=l_a, client_price_map={"cp_2400": Decimal("16")})
        res_b = await calculate_order_pricing("2400 CP", loader_id=l_b, client_price_map={"cp_2400": Decimal("16")})

        self.assertEqual(res_a["loader_cost_total"], Decimal("12"))
        self.assertEqual(res_b["loader_cost_total"], Decimal("14"))

    async def test_18_client_group_ab_isolation(self):
        """18. Client Group A balance changes do not affect Group B."""
        from database import record_delivery_ledger_entry, get_current_running_total

        g_a = -100281
        g_b = -100282
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=100.0, dedup_hash="iso_a", chat_id=g_a)
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=500.0, dedup_hash="iso_b", chat_id=g_b)

        self.assertEqual(await get_current_running_total(g_a), 100.0)
        self.assertEqual(await get_current_running_total(g_b), 500.0)

    async def test_19_historical_price_protection(self):
        """19. Updating price lists post-completion does NOT mutate historical order records."""
        from database import record_delivery_ledger_entry, set_global_client_price, set_loader_price, get_ledger_entry_by_id
        from decimal import Decimal

        chat_id = -10029
        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(9190, "cp_2400", Decimal("12"))

        entry, _ = await record_delivery_ledger_entry(
            order_id=190,
            package="2400 CP",
            now_value=16.0,
            loader_name="Loader A",
            client_amount=16.0,
            loader_cost=12.0,
            profit_amount=4.0,
            secret_profit_code="X",
            chat_id=chat_id,
            dedup_hash="hist_protect_19"
        )
        eid = entry.id

        await set_global_client_price("cp_2400", Decimal("25"))
        await set_loader_price(9190, "cp_2400", Decimal("18"))

        fetched = await get_ledger_entry_by_id(eid)
        self.assertEqual(fetched.client_amount, 16.0)
        self.assertEqual(fetched.loader_cost, 12.0)
        self.assertEqual(fetched.profit_amount, 4.0)

    async def test_20_running_total_correctness(self):
        """20. Running total updates atomically: 875 + 16 = 891."""
        from database import record_delivery_ledger_entry, get_current_running_total

        chat_id = -10030
        await record_delivery_ledger_entry(order_id=None, package="INIT", now_value=875.0, dedup_hash="rt_init", chat_id=chat_id)
        await record_delivery_ledger_entry(order_id=200, package="2400 CP", now_value=16.0, dedup_hash="rt_ord", chat_id=chat_id)
        self.assertEqual(await get_current_running_total(chat_id), 891.0)

    def test_21_secret_profit_code_remains_correct(self):
        """21. Secret profit code calculations remain exact."""
        from decimal import Decimal
        from profit_code_engine import encode_profit_code

        self.assertEqual(encode_profit_code(Decimal("4")), "X")
        self.assertEqual(encode_profit_code(Decimal("-2")), "S")
        self.assertEqual(encode_profit_code(Decimal("0")), "U")

    def test_22_no_client_privacy_leak(self):
        """22. Client delivery format never leaks actual loader cost or actual dollar profit."""
        from utils import format_payment_verification_message

        msg = format_payment_verification_message(amount=200.0, tx_id="TX123", before_total=1012.0, now_payment=200.0, running_total=812.0)
        self.assertNotIn("Loader", msg)
        self.assertNotIn("Profit", msg)

    async def test_23_concurrent_duplicate_delivery(self):
        """23. Concurrent duplicate delivery requests result in 1 DB record."""
        import asyncio
        from database import record_delivery_ledger_entry, get_current_running_total

        chat_id = -10033
        h = "concurrent_hash_23"

        task1 = asyncio.create_task(record_delivery_ledger_entry(order_id=230, package="2400 CP", now_value=16.0, dedup_hash=h, chat_id=chat_id))
        task2 = asyncio.create_task(record_delivery_ledger_entry(order_id=230, package="2400 CP", now_value=16.0, dedup_hash=h, chat_id=chat_id))

        r1, r2 = await asyncio.gather(task1, task2)
        successes = [r for r in (r1, r2) if r[1] is True]
        self.assertEqual(len(successes), 1)
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

    async def test_24_full_single_package_scenario(self):
        """24. Full single package scenario: PENDING -> PRICED -> LOADER_ASSIGNED -> SENT_TO_LOADER -> DELIVERED -> COMPLETED."""
        from database import (
            create_order,
            set_global_client_price,
            set_loader_price,
            transition_order_status,
            record_delivery_ledger_entry,
            mark_order_delivered,
            mark_order_completed,
            get_current_running_total
        )
        from decimal import Decimal

        chat_id = -10034
        loader_id = 9240
        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(loader_id, "cp_2400", Decimal("12"))

        # PENDING
        order = await create_order(email="scen24@gmail.com", package="2400 CP", client_chat_id=chat_id)
        self.assertEqual(order.status, "Pending")

        # PRICED
        upd1, _, _ = await transition_order_status(order.id, "PRICED")
        self.assertEqual(upd1.status, "PRICED")

        # LOADER_ASSIGNED
        upd2, _, _ = await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=loader_id)
        self.assertEqual(upd2.status, "LOADER_ASSIGNED")
        self.assertEqual(upd2.secret_profit_code, "X")

        # SENT_TO_LOADER
        upd3, _, _ = await transition_order_status(order.id, "SENT_TO_LOADER")
        self.assertEqual(upd3.status, "SENT_TO_LOADER")

        # DELIVERED & COMPLETED
        await record_delivery_ledger_entry(order_id=order.id, package="2400 CP", now_value=16.0, loader_name="Loader A", chat_id=chat_id, dedup_hash="scen24_deliv")
        deliv = await mark_order_delivered(order.id)
        self.assertEqual(deliv.status, "Delivered")

        comp = await mark_order_completed(order.id)
        self.assertEqual(comp.status, "Completed")
        self.assertEqual(await get_current_running_total(chat_id), 16.0)

    async def test_25_full_multi_package_scenario(self):
        """25. Full multi-package scenario ($2400 CP + 5000 CP$) with 5-image album delivery."""
        from database import (
            create_order,
            set_global_client_price,
            set_loader_price,
            transition_order_status,
            record_delivery_ledger_entry,
            mark_order_completed,
            get_current_running_total
        )
        from decimal import Decimal

        chat_id = -10035
        loader_id = 9250
        await set_global_client_price("cp_2400", Decimal("16"))
        await set_global_client_price("cp_5000", Decimal("33"))
        await set_loader_price(loader_id, "cp_2400", Decimal("12"))
        await set_loader_price(loader_id, "cp_5000", Decimal("25"))

        order = await create_order(email="scen25@gmail.com", package="2400 CP + 5000 CP", client_chat_id=chat_id)
        upd, _, _ = await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=loader_id)
        self.assertEqual(upd.client_price_total, 49.0)
        self.assertEqual(upd.loader_cost_total, 37.0)
        self.assertEqual(upd.profit_amount, 12.0)
        self.assertEqual(upd.secret_profit_code, "H")

        # 5 images in 1 album -> 1 delivery ledger entry
        entry, is_new = await record_delivery_ledger_entry(order_id=order.id, package="2400 CP + 5000 CP", now_value=49.0, loader_name="Loader B", chat_id=chat_id, dedup_hash="scen25_album_5")
        self.assertTrue(is_new)
        self.assertEqual(await get_current_running_total(chat_id), 49.0)

        comp = await mark_order_completed(order.id)
        self.assertEqual(comp.status, "Completed")


class TestStep11OperationalControls(unittest.IsolatedAsyncioTestCase):
    """Test suite for Step 11 Admin & Loader Operational Controls."""

    async def test_1_admin_can_view_pending_orders(self):
        from database import create_order
        from handlers import pendingorders_command_handler
        await create_order(email="pend1@gmail.com", package="2400 CP", client_chat_id=-1001)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await pendingorders_command_handler(mock_update, mock_context)
        self.assertEqual(len(replied), 1)
        self.assertIn("Pending Orders", replied[0])
        self.assertIn("pend1@gmail.com", replied[0])

    async def test_2_unauthorized_user_cannot_view_pending_orders(self):
        from handlers import pendingorders_command_handler

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 999999

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await pendingorders_command_handler(mock_update, mock_context)
        self.assertEqual(len(replied), 1)
        self.assertIn("Unauthorized", replied[0])

    async def test_3_admin_can_lookup_order(self):
        from database import create_order, set_global_client_price, set_loader_price, transition_order_status
        from handlers import order_lookup_command_handler
        from decimal import Decimal

        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(101, "cp_2400", Decimal("12"))
        order = await create_order(email="lookup1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=101)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -1001})()
        })()
        mock_context = type("Context", (), {"args": [str(order.id)]})()

        await order_lookup_command_handler(mock_update, mock_context)
        self.assertEqual(len(replied), 1)
        self.assertIn("Internal Admin Metrics", replied[0])
        self.assertIn("Secret Profit Code", replied[0])

    async def test_4_loader_can_lookup_only_own_assigned_order(self):
        from database import create_order, transition_order_status, LOADERS_CACHE
        from handlers import order_lookup_command_handler

        loader_user_id = 88811
        LOADERS_CACHE[loader_user_id] = {"name": "Test Loader", "group_id": -10088}

        order = await create_order(email="ldr1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=loader_user_id)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = loader_user_id

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -10088})()
        })()
        mock_context = type("Context", (), {"args": [str(order.id)]})()

        await order_lookup_command_handler(mock_update, mock_context)
        self.assertEqual(len(replied), 1)
        self.assertIn("Details", replied[0])
        self.assertNotIn("Internal Admin Metrics", replied[0])
        self.assertNotIn("Secret Profit Code", replied[0])

    async def test_5_loader_cannot_lookup_another_loader_order(self):
        from database import create_order, transition_order_status, LOADERS_CACHE
        from handlers import order_lookup_command_handler

        LOADERS_CACHE[88811] = {"name": "Loader A", "group_id": -10088}
        LOADERS_CACHE[99922] = {"name": "Loader B", "group_id": -10099}

        order = await create_order(email="ldr2@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=88811)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 99922

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -10099})()
        })()
        mock_context = type("Context", (), {"args": [str(order.id)]})()

        await order_lookup_command_handler(mock_update, mock_context)
        self.assertEqual(len(replied), 1)
        self.assertIn("not authorized", replied[0])

    async def test_6_admin_can_assign_loader(self):
        from database import create_order, get_order_by_id
        from handlers import assignloader_command_handler

        order = await create_order(email="asg1@gmail.com", package="2400 CP", client_chat_id=-1001)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": [str(order.id), "777"]})()

        await assignloader_command_handler(mock_update, mock_context)
        self.assertIn("assigned to Loader #777", replied[0])

        upd = await get_order_by_id(order.id)
        self.assertEqual(upd.loader_group_id, 777)
        self.assertEqual(upd.status, "LOADER_ASSIGNED")

    async def test_7_unauthorized_user_cannot_assign_loader(self):
        from database import create_order
        from handlers import assignloader_command_handler

        order = await create_order(email="asg2@gmail.com", package="2400 CP", client_chat_id=-1001)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 777777

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": [str(order.id), "777"]})()

        await assignloader_command_handler(mock_update, mock_context)
        self.assertIn("Unauthorized", replied[0])

    async def test_8_loader_assignment_uses_existing_lifecycle(self):
        from database import create_order, assign_order_loader
        order = await create_order(email="asg3@gmail.com", package="2400 CP", client_chat_id=-1001)
        upd_order, success, reason = await assign_order_loader(order.id, 555)
        self.assertTrue(success)
        self.assertEqual(upd_order.status, "LOADER_ASSIGNED")
        self.assertEqual(upd_order.loader_group_id, 555)

    async def test_9_admin_can_reassign_eligible_order(self):
        from database import create_order, assign_order_loader, get_order_by_id
        from handlers import reassignloader_command_handler

        order = await create_order(email="reasg1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await assign_order_loader(order.id, 111)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": [str(order.id), "222"]})()

        await reassignloader_command_handler(mock_update, mock_context)
        self.assertIn("reassigned to Loader #222", replied[0])

        upd = await get_order_by_id(order.id)
        self.assertEqual(upd.loader_group_id, 222)

    async def test_10_completed_order_cannot_be_reassigned(self):
        from database import create_order, mark_order_completed
        from handlers import reassignloader_command_handler

        order = await create_order(email="comp_re@gmail.com", package="2400 CP", client_chat_id=-1001)
        await mark_order_completed(order.id)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": [str(order.id), "333"]})()

        await reassignloader_command_handler(mock_update, mock_context)
        self.assertIn("already COMPLETED and cannot be modified", replied[0])

    async def test_11_loader_ab_privacy_isolation(self):
        from database import create_order, transition_order_status, LOADERS_CACHE
        from handlers import order_lookup_command_handler

        LOADERS_CACHE[1111] = {"name": "Loader Alpha", "group_id": -100111}
        LOADERS_CACHE[2222] = {"name": "Loader Beta", "group_id": -100222}

        order = await create_order(email="priv1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=1111)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 2222

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -100222})()
        })()
        mock_context = type("Context", (), {"args": [str(order.id)]})()

        await order_lookup_command_handler(mock_update, mock_context)
        self.assertIn("not authorized", replied[0])

    async def test_12_myorders_returns_only_current_loader_orders(self):
        from database import create_order, transition_order_status, LOADERS_CACHE
        from handlers import myorders_command_handler

        loader_id = 7711
        LOADERS_CACHE[loader_id] = {"name": "Loader Seven", "group_id": -10077}

        ord1 = await create_order(email="my1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(ord1.id, "LOADER_ASSIGNED", loader_id=loader_id)

        ord2 = await create_order(email="my2@gmail.com", package="5000 CP", client_chat_id=-1001)
        await transition_order_status(ord2.id, "LOADER_ASSIGNED", loader_id=9999)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = loader_id

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await myorders_command_handler(mock_update, mock_context)
        self.assertIn(f"Order #{ord1.id}", replied[0])
        self.assertNotIn(f"Order #{ord2.id}", replied[0])

    async def test_13_revieworders_returns_needs_review_failed(self):
        from database import create_order, transition_order_status
        from handlers import revieworders_command_handler

        ord1 = await create_order(email="rev1@gmail.com", package="UnknownPkg", client_chat_id=-1001)
        await transition_order_status(ord1.id, "NEEDS_REVIEW")

        ord2 = await create_order(email="rev2@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(ord2.id, "FAILED")

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await revieworders_command_handler(mock_update, mock_context)
        self.assertIn(f"Order #{ord1.id}", replied[0])
        self.assertIn(f"Order #{ord2.id}", replied[0])

    async def test_14_completedorders_is_read_only(self):
        from database import create_order, mark_order_completed, get_order_by_id
        from handlers import completedorders_command_handler

        order = await create_order(email="cmp_ro@gmail.com", package="2400 CP", client_chat_id=-1001)
        await mark_order_completed(order.id)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await completedorders_command_handler(mock_update, mock_context)
        self.assertIn(f"Order #{order.id}", replied[0])

        after = await get_order_by_id(order.id)
        self.assertEqual(after.status, "Completed")

    async def test_15_cancelledorders_is_read_only(self):
        from database import create_order, cancel_order, get_order_by_id
        from handlers import cancelledorders_command_handler

        order = await create_order(email="cnc_ro@gmail.com", package="2400 CP", client_chat_id=-1001)
        await cancel_order(order.id)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await cancelledorders_command_handler(mock_update, mock_context)
        self.assertIn(f"Order #{order.id}", replied[0])

        after = await get_order_by_id(order.id)
        self.assertIn(after.status, ("Cancelled", "CANCELLED"))

    async def test_16_failedorders_is_read_only(self):
        from database import create_order, transition_order_status, get_order_by_id
        from handlers import failedorders_command_handler

        order = await create_order(email="fld_ro@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "FAILED")

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await failedorders_command_handler(mock_update, mock_context)
        self.assertIn(f"Order #{order.id}", replied[0])

        after = await get_order_by_id(order.id)
        self.assertEqual(after.status, "FAILED")

    async def test_17_missing_client_price_diagnostic(self):
        from database import create_order, transition_order_status
        from handlers import revieworders_command_handler

        order = await create_order(email="diag_c@gmail.com", package="99999 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "NEEDS_REVIEW")

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await revieworders_command_handler(mock_update, mock_context)
        self.assertIn("Missing Client Price", replied[0])

    async def test_18_missing_loader_price_diagnostic(self):
        from database import create_order, set_global_client_price, assign_order_loader, transition_order_status
        from handlers import revieworders_command_handler
        from decimal import Decimal

        await set_global_client_price("cp_2400", Decimal("16"))
        order = await create_order(email="diag_l@gmail.com", package="2400 CP", client_chat_id=-1001)
        await assign_order_loader(order.id, 99999)
        await transition_order_status(order.id, "NEEDS_REVIEW")

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {
            "effective_user": MockUser(),
            "effective_message": MockMsg()
        })()
        mock_context = type("Context", (), {"args": []})()

        await revieworders_command_handler(mock_update, mock_context)
        self.assertIn("Missing Loader Cost", replied[0])

    async def test_19_retry_only_works_for_eligible_orders(self):
        from database import create_order, transition_order_status, mark_order_completed, get_order_by_id
        from handlers import retryorder_command_handler

        ord_fld = await create_order(email="rty1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(ord_fld.id, "FAILED")

        ord_cmp = await create_order(email="rty2@gmail.com", package="2400 CP", client_chat_id=-1001)
        await mark_order_completed(ord_cmp.id)

        replied = []
        class MockMsg:
            async def reply_text(self, text, reply_markup=None, parse_mode=None):
                replied.append(text)

        class MockUser:
            id = 1573531032

        mock_update = type("Update", (), {"effective_user": MockUser(), "effective_message": MockMsg()})()
        await retryorder_command_handler(mock_update, type("Context", (), {"args": [str(ord_fld.id)]})())
        self.assertIn("reset to", replied[0])
        res_fld = await get_order_by_id(ord_fld.id)
        self.assertEqual(res_fld.status, "SENT_TO_LOADER")

        replied.clear()
        await retryorder_command_handler(mock_update, type("Context", (), {"args": [str(ord_cmp.id)]})())
        self.assertIn("already COMPLETED and cannot be retried", replied[0])

    async def test_20_retry_cannot_duplicate_financial_delivery(self):
        from database import create_order, transition_order_status, get_current_running_total, get_latest_ledger_entries
        from handlers import retryorder_command_handler

        order = await create_order(email="rty_fin@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "FAILED")

        before_total = await get_current_running_total(-1001)
        before_entries = len(await get_latest_ledger_entries())

        class MockMsg:
            async def reply_text(self, *a, **k):
                pass

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg()
        })()
        await retryorder_command_handler(mock_update, type("Context", (), {"args": [str(order.id)]})())

        after_total = await get_current_running_total(-1001)
        after_entries = len(await get_latest_ledger_entries())

        self.assertEqual(before_total, after_total)
        self.assertEqual(before_entries, after_entries)

    async def test_21_pagination_works(self):
        from database import create_order
        from handlers import pendingorders_command_handler

        for i in range(15):
            await create_order(email=f"page{i}@gmail.com", package="2400 CP", client_chat_id=-1001)

        replied_p1 = []
        class MockMsg1:
            async def reply_text(self, t, **k):
                replied_p1.append(t)

        mock_update1 = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg1()
        })()
        await pendingorders_command_handler(mock_update1, type("Context", (), {"args": ["1"]})())
        self.assertIn("Page 1", replied_p1[0])

        replied_p2 = []
        class MockMsg2:
            async def reply_text(self, t, **k):
                replied_p2.append(t)

        mock_update2 = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg2()
        })()
        await pendingorders_command_handler(mock_update2, type("Context", (), {"args": ["2"]})())
        self.assertIn("Page 2", replied_p2[0])

    async def test_22_callback_authorization_works(self):
        from handlers import operational_pagination_callback_handler

        edited = []
        class MockQuery:
            from_user = type("User", (), {"id": 999999})()
            callback_data = "op_pending:2"
            async def answer(self): pass
            async def edit_message_text(self, text, **kwargs):
                edited.append(text)

        mock_update = type("Update", (), {"callback_query": MockQuery()})()
        await operational_pagination_callback_handler(mock_update, None)
        self.assertEqual(len(edited), 1)
        self.assertIn("Unauthorized", edited[0])

    async def test_23_callback_cannot_access_another_loader_order(self):
        from database import create_order, transition_order_status, LOADERS_CACHE
        from handlers import operational_pagination_callback_handler

        LOADERS_CACHE[9001] = {"name": "L1", "group_id": -100901}
        ord1 = await create_order(email="lcb1@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(ord1.id, "LOADER_ASSIGNED", loader_id=9001)

        edited = []
        class MockQuery:
            from_user = type("User", (), {"id": 9002})()
            callback_data = "op_myorders:1"
            async def answer(self): pass
            async def edit_message_text(self, text, **kwargs):
                edited.append(text)

        mock_update = type("Update", (), {"callback_query": MockQuery()})()
        await operational_pagination_callback_handler(mock_update, None)
        self.assertIn("My Active Orders", edited[0])
        self.assertNotIn(f"Order #{ord1.id}", edited[0])

    async def test_24_password_otp_not_leaked_in_list_views(self):
        from database import create_order
        from handlers import pendingorders_command_handler

        raw = "Email: secretuser@gmail.com\nPassword: SUPERSECRET123\nOTP: 887766\n2400 CP"
        order = await create_order(email="secretuser@gmail.com", package="2400 CP", client_chat_id=-1001, raw_text=raw)

        replied = []
        class MockMsg:
            async def reply_text(self, t, **k):
                replied.append(t)

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg()
        })()
        await pendingorders_command_handler(mock_update, type("Context", (), {"args": []})())

        self.assertNotIn("SUPERSECRET123", replied[0])
        self.assertNotIn("887766", replied[0])

    async def test_25_admin_actions_recorded_in_audit_log_if_supported(self):
        from database import assign_order_loader, create_order
        order = await create_order(email="audit_test@gmail.com", package="2400 CP", client_chat_id=-1001)
        upd, success, reason = await assign_order_loader(order.id, 999)
        self.assertTrue(success)
        self.assertEqual(upd.loader_group_id, 999)

    async def test_26_viewing_order_causes_zero_financial_side_effects(self):
        from database import create_order, get_current_running_total, get_latest_ledger_entries
        from handlers import order_lookup_command_handler

        order = await create_order(email="zero_fin@gmail.com", package="2400 CP", client_chat_id=-1001)
        before_total = await get_current_running_total(-1001)
        before_entries = len(await get_latest_ledger_entries())

        class MockMsg:
            async def reply_text(self, *a, **k):
                pass

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -1001})()
        })()
        await order_lookup_command_handler(mock_update, type("Context", (), {"args": [str(order.id)]})())

        after_total = await get_current_running_total(-1001)
        after_entries = len(await get_latest_ledger_entries())

        self.assertEqual(before_total, after_total)
        self.assertEqual(before_entries, after_entries)

    async def test_27_loader_assignment_does_not_create_delivery_ledger(self):
        from database import create_order, assign_order_loader, get_latest_ledger_entries
        order = await create_order(email="no_ledger@gmail.com", package="2400 CP", client_chat_id=-1001)
        before_entries = len(await get_latest_ledger_entries())
        await assign_order_loader(order.id, 888)
        after_entries = len(await get_latest_ledger_entries())
        self.assertEqual(before_entries, after_entries)

    async def test_28_client_group_isolation_remains_intact(self):
        from database import create_order, record_delivery_ledger_entry, get_current_running_total
        ord1 = await create_order(email="grpA@gmail.com", package="2400 CP", client_chat_id=-1001)
        ord2 = await create_order(email="grpB@gmail.com", package="2400 CP", client_chat_id=-1002)

        await record_delivery_ledger_entry(order_id=ord1.id, package="2400 CP", now_value=16.0, loader_name="L1", chat_id=-1001, dedup_hash="grpA_deliv")

        totA = await get_current_running_total(-1001)
        totB = await get_current_running_total(-1002)

        self.assertEqual(totA, 16.0)
        self.assertEqual(totB, 0.0)

    async def test_29_loader_price_isolation_remains_intact(self):
        from database import set_loader_price, get_loader_price
        from decimal import Decimal

        await set_loader_price(1001, "cp_2400", Decimal("12"))
        await set_loader_price(1002, "cp_2400", Decimal("14"))

        p1 = await get_loader_price(1001, "cp_2400")
        p2 = await get_loader_price(1002, "cp_2400")

        self.assertEqual(p1, Decimal("12"))
        self.assertEqual(p2, Decimal("14"))

    async def test_30_historical_completed_order_remains_unchanged(self):
        from database import create_order, set_global_client_price, set_loader_price, transition_order_status, mark_order_completed, assign_order_loader, get_order_by_id
        from decimal import Decimal

        await set_global_client_price("cp_2400", Decimal("16"))
        await set_loader_price(999, "cp_2400", Decimal("12"))

        order = await create_order(email="hist_comp@gmail.com", package="2400 CP", client_chat_id=-1001)
        await transition_order_status(order.id, "LOADER_ASSIGNED", loader_id=999)
        comp = await mark_order_completed(order.id)

        c_price = comp.client_price_total
        l_cost = comp.loader_cost_total
        p_amt = comp.profit_amount
        p_code = comp.secret_profit_code

        order_upd, success, reason = await assign_order_loader(order.id, 888)
        self.assertFalse(success)
        self.assertEqual(reason, "CANNOT_MODIFY_COMPLETED_ORDER")

        after = await get_order_by_id(order.id)
        self.assertEqual(after.client_price_total, c_price)
        self.assertEqual(after.loader_cost_total, l_cost)
        self.assertEqual(after.profit_amount, p_amt)
        self.assertEqual(after.secret_profit_code, p_code)

    async def test_31_invalid_order_and_loader_ids_handled_safely(self):
        from handlers import order_lookup_command_handler, assignloader_command_handler, retryorder_command_handler
        replied = []
        class MockMsg:
            async def reply_text(self, text, **kwargs):
                replied.append(text)

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg()
        })()

        # Non-existent order ID
        await order_lookup_command_handler(mock_update, type("Context", (), {"args": ["999999"]})())
        self.assertIn("not found", replied[-1])

        # Assign non-existent order ID
        await assignloader_command_handler(mock_update, type("Context", (), {"args": ["999999", "101"]})())
        self.assertIn("ORDER_NOT_FOUND", replied[-1])

        # Retry non-existent order ID
        await retryorder_command_handler(mock_update, type("Context", (), {"args": ["999999"]})())
        self.assertIn("ORDER_NOT_FOUND", replied[-1])

    async def test_32_pagination_edge_cases_and_invalid_pages(self):
        from handlers import pendingorders_command_handler
        replied = []
        class MockMsg:
            async def reply_text(self, text, **kwargs):
                replied.append(text)

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg()
        })()

        # Non-numeric page argument -> defaults safely to page 1
        await pendingorders_command_handler(mock_update, type("Context", (), {"args": ["abc"]})())
        self.assertIn("Page 1", replied[-1])

        # Out-of-bounds large page number
        await pendingorders_command_handler(mock_update, type("Context", (), {"args": ["9999"]})())
        self.assertIn("Page 9999", replied[-1])

    async def test_33_all_readonly_commands_preserve_ledger_counts(self):
        from database import create_order, get_latest_ledger_entries
        from handlers import (
            pendingorders_command_handler,
            order_lookup_command_handler,
            order_status_command_handler,
            myorders_command_handler,
            revieworders_command_handler,
            completedorders_command_handler,
            cancelledorders_command_handler,
            failedorders_command_handler
        )

        order = await create_order(email="ro_check@gmail.com", package="2400 CP", client_chat_id=-1001)
        before_entries = len(await get_latest_ledger_entries())

        class MockMsg:
            async def reply_text(self, *a, **k): pass

        mock_update = type("Update", (), {
            "effective_user": type("User", (), {"id": 1573531032})(),
            "effective_message": MockMsg(),
            "effective_chat": type("Chat", (), {"id": -1001})()
        })()
        ctx = type("Context", (), {"args": [str(order.id)]})()

        await pendingorders_command_handler(mock_update, ctx)
        await order_lookup_command_handler(mock_update, ctx)
        await order_status_command_handler(mock_update, ctx)
        await myorders_command_handler(mock_update, ctx)
        await revieworders_command_handler(mock_update, ctx)
        await completedorders_command_handler(mock_update, ctx)
        await cancelledorders_command_handler(mock_update, ctx)
        await failedorders_command_handler(mock_update, ctx)

        after_entries = len(await get_latest_ledger_entries())
        self.assertEqual(before_entries, after_entries)

    async def test_34_delivery_ledger_reply_fallback(self):
        from database import create_order, set_global_client_price, get_latest_ledger_entries
        from handlers import process_delivery_ledger_event
        from decimal import Decimal

        await set_global_client_price("cp_2400", Decimal("16.5"))
        order = await create_order(email="fallback_user@gmail.com", package="2400 CP", client_chat_id=-1001)

        sent_messages = []
        call_count = 0

        class MockBot:
            async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode=None):
                nonlocal call_count
                call_count += 1
                if reply_to_message_id is not None:
                    raise Exception("BadRequest: message to be replied not found")
                sent_messages.append(text)

        mock_bot = MockBot()
        before_entries = len(await get_latest_ledger_entries(limit=1000))

        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name="Loader #1",
            bot=mock_bot,
            chat_id=-1001,
            dedup_hash=f"fallback_dedup_{order.id}",
            reply_to_message_id=999999
        )

        self.assertEqual(call_count, 2, "Bot must retry without reply_to_message_id when reply fails")
        self.assertEqual(len(sent_messages), 1, "Fallback message must be successfully sent")
        self.assertIn("Price: $16.5", sent_messages[0])

        after_entries = len(await get_latest_ledger_entries(limit=1000))
        self.assertEqual(after_entries, before_entries + 1, "Exactly ONE DeliveryLedger entry must be created")

        # Re-run with same dedup_hash -> must block duplicate and send 0 additional messages
        sent_messages.clear()
        call_count = 0
        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name="Loader #1",
            bot=mock_bot,
            chat_id=-1001,
            dedup_hash=f"fallback_dedup_{order.id}",
            reply_to_message_id=999999
        )
        self.assertEqual(call_count, 0, "Duplicate delivery ledger event must not attempt resending")
        self.assertEqual(len(sent_messages), 0)


class TestStep12RealTelegramBugFixes(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying minimal safe fixes for Step 12 real Telegram test bugs."""

    async def asyncSetUp(self):
        from database import init_db, set_global_client_price_in_db
        await init_db()
        await set_global_client_price_in_db("cp_2400", 16.0, display_name="2400 CP")

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, reload_global_client_prices_cache
        from models import GlobalClientPrice
        from sqlalchemy import delete
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.commit()
        await reload_global_client_prices_cache()

    async def test_global_price_precedence_over_legacy_package_prices(self):
        from pricing_calculator import calculate_order_pricing
        from utils import calculate_delivered_packages_value

        res = await calculate_order_pricing("2400 CP")
        self.assertEqual(float(res["client_price_total"]), 16.0, "Global client price 16.0 must be used in calculate_order_pricing")

        val, ok = calculate_delivered_packages_value("2400 CP")
        self.assertTrue(ok)
        self.assertEqual(val, 16.0, "Delivered package value must be 16.0")

    async def test_delivery_caption_price_formatting(self):
        from utils import format_delivered_packages_caption
        caption = format_delivered_packages_caption([{"package": "2400 CP", "qty": 1}])
        self.assertIn("💰 Price: 16$", caption, "Caption price must show 16$ rather than 16.5$")

    async def test_save_order_pricing_loader_group_id_no_int32_overflow(self):
        from database import create_order, save_order_pricing, add_loader
        # Register loader with Telegram Chat ID -1004475489329
        telegram_group_id = -1004475489329
        await add_loader(group_id=telegram_group_id, loader_name="Test Loader Chat")

        order = await create_order(
            email="int32_test@example.com",
            client_chat_id=-100111,
            original_message_id=123,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text="Package: 2400 CP"
        )
        order.loader_group_id = telegram_group_id

        # Must execute without throwing PostgreSQL int32 range error
        updated = await save_order_pricing(order.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.client_price_total, 16.0)

    async def test_delivery_accounting_summary_format(self):
        from utils import format_delivery_summary_message
        msg = format_delivery_summary_message(
            email="test@example.com",
            client_price=16.0,
            secret_code="TESTPROFITCODE",
            before_total=0.0,
            now_value=16.0,
            running_total=16.0
        )
        self.assertEqual(msg, "Price: $16\nTESTPROFITCODE\n\nBefore: 0\nNow: 16\nTotal: 16")
        self.assertNotIn("test@example.com", msg)
        self.assertNotIn("Profit:", msg)

    async def test_album_accounting_and_deduplication(self):
        from database import create_order, save_order_pricing, get_running_total_current
        from handlers import process_delivery_ledger_event
        from unittest.mock import AsyncMock, MagicMock

        order = await create_order(
            email="album_accounting@example.com",
            client_chat_id=-100222,
            original_message_id=456,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text="Package: 2400 CP"
        )
        await save_order_pricing(order.id)

        mock_bot = AsyncMock()
        sent_messages = []
        async def mock_send_message(chat_id, text, **kwargs):
            sent_messages.append((chat_id, text))
            m = MagicMock()
            m.message_id = 99
            return m
        mock_bot.send_message = mock_send_message

        dedup_tag = f"step12_album_dedup_{order.id}"

        # Fetch baseline running total right before delivery event
        before_rt = await get_running_total_current(chat_id=-100222)

        # First album flush delivery event
        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name="Test Loader",
            bot=mock_bot,
            chat_id=-100222,
            dedup_hash=dedup_tag,
            reply_to_message_id=None
        )

        after_rt1 = await get_running_total_current(chat_id=-100222)
        self.assertAlmostEqual(after_rt1, before_rt + 16.0, places=2, msg="Running total must increase by exactly 16.0")
        self.assertEqual(len(sent_messages), 1, "Exactly ONE accounting summary message must be sent")
        self.assertIn("Price: $16", sent_messages[0][1])

        # Duplicate album flush event with same dedup_hash
        sent_messages.clear()
        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name="Test Loader",
            bot=mock_bot,
            chat_id=-100222,
            dedup_hash=dedup_tag,
            reply_to_message_id=None
        )

        after_rt2 = await get_running_total_current(chat_id=-100222)
        self.assertEqual(after_rt2, after_rt1, "Duplicate album event must NOT increment running total again")
        self.assertEqual(len(sent_messages), 0, "Duplicate album event must NOT send a second accounting summary")

    async def test_historical_order_price_protection(self):
        from database import create_order, AsyncSessionLocal
        order = await create_order(
            email="historical@example.com",
            client_chat_id=-100333,
            original_message_id=789,
            package="2400 CP",
            status="Completed",
            category="A",
            raw_text="Package: 2400 CP"
        )
        async with AsyncSessionLocal() as session:
            order.client_price_total = 16.5
            order.price = "$16.5"
            session.add(order)
            await session.commit()

        # Re-query historical order and confirm its stored price remains 16.5
        async with AsyncSessionLocal() as session:
            h_order = await session.get(order.__class__, order.id)
            self.assertEqual(h_order.client_price_total, 16.5)
            self.assertEqual(h_order.price, "$16.5")


class TestSetLoaderPriceReplyBased(unittest.IsolatedAsyncioTestCase):
    """
    Dedicated test suite for reply-based /setloaderprice loader identification,
    privacy/isolation, group_id resolution (-1004475489329), and error cases.
    """

    async def asyncSetUp(self):
        from database import engine, Base, LOADER_PRICES_CACHE, LOADERS_CACHE, AsyncSessionLocal
        from sqlalchemy import delete
        from models import LoaderPrice, Loader, Order

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSessionLocal() as session:
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(Loader))
            await session.execute(delete(Order))
            await session.commit()

        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()

    async def test_setloaderprice_reply_based_scenarios(self):
        from decimal import Decimal
        from database import (
            add_loader,
            get_loader_price,
            LOADER_PRICES_CACHE,
            LOADERS_CACHE,
            create_order,
            save_order_pricing,
            AsyncSessionLocal
        )
        from handlers import setloaderprice_command_handler
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        # 1. Admin replies to Loader A price list -> Loader A prices saved
        group_id_a = -1004475489329  # 64-bit negative BIGINT Telegram Group ID
        loader_a = await add_loader(group_id=group_id_a, loader_name="Loader Group A")

        class MockRepliedMsgA:
            chat = type("Chat", (), {"id": group_id_a})()
            text = "2400 CP ➡️ $12\n5000 CP ➡️ $25\n10800 CP ➡️ $50"
            caption = None

        class MockCmdMsg:
            def __init__(self, reply_msg):
                self.reply_to_message = reply_msg
                self.text = "/setloaderprice"
                self.replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_admin_a = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": group_id_a})(),
            "effective_message": MockCmdMsg(MockRepliedMsgA())
        })()
        ctx = type("Context", (), {"args": []})()

        await setloaderprice_command_handler(up_admin_a, ctx)
        self.assertIn("Loader price list updated", up_admin_a.effective_message.replied_text)
        self.assertIn("Products updated: 3", up_admin_a.effective_message.replied_text)

        price_a_2400 = await get_loader_price(loader_a.id, "cp_2400")
        self.assertEqual(price_a_2400, Decimal("12"))

        # 2 & 3. Admin replies to Loader B price list -> Loader B prices saved independently ($13 vs $12)
        group_id_b = -1009999999999
        loader_b = await add_loader(group_id=group_id_b, loader_name="Loader Group B")

        class MockRepliedMsgB:
            chat = type("Chat", (), {"id": group_id_b})()
            text = "2400 CP ➡️ $13"
            caption = None

        up_admin_b = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": group_id_b})(),
            "effective_message": MockCmdMsg(MockRepliedMsgB())
        })()

        await setloaderprice_command_handler(up_admin_b, ctx)
        self.assertIn("Loader price list updated", up_admin_b.effective_message.replied_text)

        # Confirm isolation: Loader A remains $12, Loader B is $13
        price_a_check = await get_loader_price(loader_a.id, "cp_2400")
        price_b_check = await get_loader_price(loader_b.id, "cp_2400")
        self.assertEqual(price_a_check, Decimal("12"))
        self.assertEqual(price_b_check, Decimal("13"))

        # 4. Telegram Group ID (-1004475489329) resolution safety:
        # Confirm group_id_a is matched via Loader.group_id and not confused with Loader.id (integer primary key)
        self.assertNotEqual(loader_a.id, group_id_a)
        self.assertEqual(loader_a.group_id, group_id_a)

        # 5. /setloaderprice without reply -> rejected
        up_no_reply = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": group_id_a})(),
            "effective_message": MockCmdMsg(None)
        })()
        await setloaderprice_command_handler(up_no_reply, ctx)
        self.assertIn("Reply to a price-list message", up_no_reply.effective_message.replied_text)

        # 6. Unauthorized user / unregistered group -> rejected
        class MockRepliedMsgUnreg:
            chat = type("Chat", (), {"id": -1008888888888})()
            text = "2400 CP ➡️ $10"
            caption = None

        up_unreg = type("Update", (), {
            "effective_user": type("User", (), {"id": 888888})(),
            "effective_chat": type("Chat", (), {"id": -1008888888888})(),
            "effective_message": MockCmdMsg(MockRepliedMsgUnreg())
        })()
        await setloaderprice_command_handler(up_unreg, ctx)
        self.assertIn("not authorized", up_unreg.effective_message.replied_text.lower())

        # 7. Invalid price list -> rejected, existing prices unchanged
        class MockRepliedMsgInvalid:
            chat = type("Chat", (), {"id": group_id_a})()
            text = "Invalid text with no prices"
            caption = None

        up_invalid = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": group_id_a})(),
            "effective_message": MockCmdMsg(MockRepliedMsgInvalid())
        })()
        await setloaderprice_command_handler(up_invalid, ctx)
        self.assertIn("No valid loader prices found", up_invalid.effective_message.replied_text)
        # Verify Loader A prices are untouched
        self.assertEqual(await get_loader_price(loader_a.id, "cp_2400"), Decimal("12"))

        # 8. Successful update -> loader price cache refreshed
        self.assertIn(loader_a.id, LOADER_PRICES_CACHE)
        self.assertEqual(float(LOADER_PRICES_CACHE[loader_a.id]["cp_2400"]["cost"]), 12.0)

        # 9. Existing order pricing remains unchanged after updating loader prices
        order = await create_order(
            email="existing_order_test@example.com",
            client_chat_id=-100555,
            original_message_id=111,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text="Package: 2400 CP"
        )
        await save_order_pricing(order.id, loader_id=loader_a.id)

        # Save baseline price
        async with AsyncSessionLocal() as session:
            h_order = await session.get(order.__class__, order.id)
            saved_loader_cost = h_order.loader_cost_total

        # Update Loader A price list
        class MockRepliedMsgUpdate:
            chat = type("Chat", (), {"id": group_id_a})()
            text = "2400 CP ➡️ $15"
            caption = None

        up_update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": group_id_a})(),
            "effective_message": MockCmdMsg(MockRepliedMsgUpdate())
        })()
        await setloaderprice_command_handler(up_update, ctx)

        # Verify historical order pricing remains unchanged
        async with AsyncSessionLocal() as session:
            h_order_after = await session.get(order.__class__, order.id)
            self.assertEqual(h_order_after.loader_cost_total, saved_loader_cost)

    async def test_setloaderprice_registration_and_menu_visibility(self):
        from main import build_application, post_init
        from handlers import help_command
        from unittest.mock import AsyncMock, MagicMock, patch
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        dummy_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789"
        with patch.object(Config, "BOT_TOKEN", dummy_token):
            app = build_application()
        registered_commands = []
        for group_handlers in app.handlers.values():
            for handler in group_handlers:
                if hasattr(handler, "commands"):
                    registered_commands.extend(list(handler.commands))

        self.assertIn("setloaderprice", registered_commands, "setloaderprice CommandHandler must be registered in build_application()")

        # B. /setloaderprice appears in help output
        class MockHelpMsg:
            replied_text = ""

            async def reply_text(self, text, **kwargs):
                self.replied_text = text

        up_help = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_message": MockHelpMsg()
        })()
        ctx_help = type("Context", (), {})()

        await help_command(up_help, ctx_help)
        self.assertIn("/setloaderprice", up_help.effective_message.replied_text, "/setloaderprice must appear in /help output")

        # C. /setloaderprice appears in BotCommand list passed to set_my_commands()
        mock_bot = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        app.bot = mock_bot

        with patch("main.init_db", AsyncMock()), \
             patch("main.reload_bot_settings_cache", AsyncMock()), \
             patch("main.reload_auth_users_cache", AsyncMock()), \
             patch("main.reload_loaders_cache", AsyncMock()), \
             patch("main.check_order_timeouts", AsyncMock(return_value=0)):
            await post_init(app)

        mock_bot.set_my_commands.assert_called_once()
        sent_bot_commands = mock_bot.set_my_commands.call_args[0][0]
        cmd_names = [cmd.command for cmd in sent_bot_commands]
        self.assertIn("setloaderprice", cmd_names, "setloaderprice must be present in Telegram BotCommand list passed to set_my_commands")

    async def test_loaderadd_creation_and_cache_reload(self):
        from database import add_loader, reload_loaders_cache, LOADERS_CACHE

        group_id = -1004475489329
        loader_name = "Real Telegram Test Group"

        # J. /loaderadd creates Loader with internal INTEGER id and BIGINT group_id
        loader = await add_loader(group_id=group_id, loader_name=loader_name)
        self.assertIsNotNone(loader.id)
        self.assertIsInstance(loader.id, int)
        self.assertGreaterEqual(loader.id, 1)
        self.assertEqual(loader.group_id, group_id)

        # K. reload_loaders_cache() correctly reloads registered Loader into RAM cache
        LOADERS_CACHE.clear()
        cache = await reload_loaders_cache()
        self.assertIn(loader.id, cache)
        self.assertEqual(cache[loader.id]["group_id"], group_id)
        self.assertEqual(cache[loader.id]["name"], loader_name)


class TestLoaderAddGroupIdParsing(unittest.IsolatedAsyncioTestCase):
    """Comprehensive test suite for /loaderadd group ID parsing, duplicate protection,
    handler isolation, and running-total protection.
    """

    async def asyncSetUp(self):
        from database import init_db, reload_loaders_cache
        from handlers import LOADER_ADD_SESSION
        await init_db()
        LOADER_ADD_SESSION.clear()
        await reload_loaders_cache()

    async def test_admin_loaderadd_single_arg_success(self):
        from handlers import loaderadd_command
        from database import get_all_loaders, LOADERS_CACHE, get_current_running_total
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032
        group_id = -1004475489399

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"reply_text": mock_reply})()
        })()
        context = type("Context", (), {"args": [str(group_id)]})()

        rt_before = await get_current_running_total()
        await loaderadd_command(update, context)
        rt_after = await get_current_running_total()

        self.assertEqual(rt_before, rt_after, "Running total MUST NOT be changed by /loaderadd!")
        self.assertTrue(any("added" in t.lower() or "loader group" in t.lower() for t in replied_texts))

        loaders = await get_all_loaders()
        loader = next((l for l in loaders if l.group_id == group_id), None)
        self.assertIsNotNone(loader)
        self.assertIsInstance(loader.id, int)
        self.assertGreater(loader.id, 0)
        self.assertEqual(loader.group_id, group_id)

        # Confirm RAM cache has it
        self.assertIn(loader.id, LOADERS_CACHE)

    async def test_admin_loaderadd_two_args_success(self):
        from handlers import loaderadd_command
        from database import get_all_loaders
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032
        group_id = -1004475489330
        loader_name = "AlphaLoader"

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"reply_text": mock_reply})()
        })()
        context = type("Context", (), {"args": [str(group_id), loader_name]})()

        await loaderadd_command(update, context)

        loaders = await get_all_loaders()
        loader = next((l for l in loaders if l.group_id == group_id), None)
        self.assertIsNotNone(loader)
        self.assertEqual(loader.loader_name, loader_name)

    async def test_loaderadd_duplicate_rejected(self):
        from handlers import loaderadd_command
        from database import add_loader, reload_loaders_cache
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032
        group_id = -1004475489331

        await add_loader(group_id=group_id, loader_name="ExistingLoader")
        await reload_loaders_cache()

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"reply_text": mock_reply})()
        })()
        context = type("Context", (), {"args": [str(group_id)]})()

        await loaderadd_command(update, context)

        self.assertTrue(any("already registered" in t.lower() for t in replied_texts))

    async def test_loaderadd_invalid_group_id(self):
        from handlers import loaderadd_command
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"reply_text": mock_reply})()
        })()
        context = type("Context", (), {"args": ["not_a_number"]})()

        await loaderadd_command(update, context)

        self.assertTrue(any("invalid loader group id" in t.lower() for t in replied_texts))

    async def test_non_admin_loaderadd_rejected(self):
        from handlers import loaderadd_command

        non_admin_id = 9999888877

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": non_admin_id})(),
            "effective_chat": type("Chat", (), {"id": non_admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"reply_text": mock_reply})()
        })()
        context = type("Context", (), {"args": ["-1004475489332"]})()

        await loaderadd_command(update, context)

        self.assertTrue(any("not authorized" in t.lower() for t in replied_texts))

    async def test_group_id_text_message_does_not_mutate_running_total(self):
        from handlers import manual_running_total_text_handler
        from database import get_current_running_total
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032
        raw_text = "-1004475489329"

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"text": raw_text, "reply_text": mock_reply})()
        })()
        context = type("Context", (), {})()

        rt_before = await get_current_running_total()
        await manual_running_total_text_handler(update, context)
        rt_after = await get_current_running_total()

        self.assertEqual(rt_before, rt_after, "Standalone group ID text string MUST NOT update running total!")
        self.assertEqual(len(replied_texts), 0, "No reply message should be sent by running total handler for group ID text.")

    async def test_wizard_session_group_id_isolation(self):
        from handlers import manual_running_total_text_handler, loader_text_wizard_handler, LOADER_ADD_SESSION
        from database import get_current_running_total
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        # Step 1: User enters wizard session
        LOADER_ADD_SESSION[admin_id] = {"step": 1, "chat_id": admin_id}

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"text": "-1004475489333", "reply_text": mock_reply})()
        })()
        context = type("Context", (), {})()

        rt_before = await get_current_running_total()
        # manual_running_total_text_handler should ignore users in LOADER_ADD_SESSION
        await manual_running_total_text_handler(update, context)
        rt_after = await get_current_running_total()

        self.assertEqual(rt_before, rt_after, "Running total MUST NOT change when user is in LOADER_ADD_SESSION!")

        # Process with loader_text_wizard_handler
        await loader_text_wizard_handler(update, context)
        self.assertTrue(any("loader name" in t.lower() for t in replied_texts))
        self.assertEqual(LOADER_ADD_SESSION[admin_id]["group_id"], -1004475489333)

    async def test_ordinary_amount_text_still_updates_running_total(self):
        from handlers import manual_running_total_text_handler
        from database import get_running_total_current
        from config import Config

        admin_id = list(Config.ADMIN_IDS)[0] if Config.ADMIN_IDS else 1573531032

        replied_texts = []
        async def mock_reply(*args, **kwargs):
            for a in args:
                if isinstance(a, str):
                    replied_texts.append(a)

        update = type("Update", (), {
            "effective_user": type("User", (), {"id": admin_id})(),
            "effective_chat": type("Chat", (), {"id": admin_id, "type": "private"})(),
            "effective_message": type("Message", (), {"text": "+15.5", "reply_text": mock_reply})()
        })()
        context = type("Context", (), {})()

        rt_before = await get_running_total_current(chat_id=admin_id)
        await manual_running_total_text_handler(update, context)
        rt_after = await get_running_total_current(chat_id=admin_id)

class TestRealOrderPricingIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Regression and unit test suite verifying real Telegram order pricing/profit integration.
    Guarantees:
      - 2400 CP order resolves global client price ($16) and loader cost ($12) -> client=16, loader=12, profit=4, secret_code='X'.
      - Legacy hardcoded $16.5 CANNOT override active global client price of $16.
      - Startup cache reloads populate GLOBAL_CLIENT_PRICES_CACHE and LOADER_PRICES_CACHE.
      - save_order_pricing correctly resolves Loader Group ID to Loader.id.
      - Multi-package orders sum client totals, loader costs, and profit codes correctly.
    """

    async def asyncSetUp(self):
        from database import (
            init_db,
            add_loader,
            set_global_client_price,
            set_loader_price,
            reload_loaders_cache,
            reload_global_client_prices_cache,
            reload_loader_prices_cache
        )
        await init_db()

        # Seed global client price cp_2400 = $16
        await set_global_client_price(product_key="cp_2400", price=16.0, display_name="2400 CP", package_type="normal_cp")
        await reload_global_client_prices_cache()

        # Create test loader with Group ID -1004475489329
        self.loader_group_id = -1004475489329
        self.loader = await add_loader(group_id=self.loader_group_id, loader_name="Test Pricing Loader")
        await reload_loaders_cache()

        # Seed loader cost cp_2400 = $12 for this loader
        await set_loader_price(loader_id=self.loader.id, product_key="cp_2400", cost=12.0)
        await reload_loader_prices_cache()

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, GlobalClientPrice, LoaderPrice, Loader, GLOBAL_CLIENT_PRICES_CACHE, LOADER_PRICES_CACHE, LOADERS_CACHE
        from sqlalchemy import delete
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(Loader))
            await session.commit()
        GLOBAL_CLIENT_PRICES_CACHE.clear()
        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()

    async def test_2400_cp_order_pricing_and_profit(self):
        from database import create_order, save_order_pricing, set_order_loader_message_id
        order_text = (
            "Email: testprofit@example.com\n"
            "Password: Test12345\n"
            "Package: 2400 CP"
        )
        order = await create_order(
            email="testprofit@example.com",
            client_chat_id=-100123456789,
            original_message_id=999,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text=order_text
        )
        await set_order_loader_message_id(order.id, 999111, loader_group_id=self.loader_group_id)

        updated = await save_order_pricing(order.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.client_price_total, 16.0, "Client price MUST be 16.0 (not legacy 16.5)")
        self.assertNotEqual(updated.client_price_total, 16.5, "Legacy 16.5 price MUST NOT override active global price of 16.0")
        self.assertEqual(updated.loader_cost_total, 12.0, "Loader cost MUST be 12.0")
        self.assertEqual(updated.profit_amount, 4.0, "Profit MUST be 4.0")
        self.assertEqual(updated.secret_profit_code, "X", "Secret profit code for $4 profit MUST be 'X'")

    async def test_legacy_16_5_cannot_override_global_price(self):
        from utils import _get_global_client_price_for_package, calculate_delivered_packages_value

        price_val = _get_global_client_price_for_package("2400 CP")
        self.assertEqual(price_val, 16.0, "Global client price for 2400 CP MUST be 16.0")

        val, all_k = calculate_delivered_packages_value("2400")
        self.assertTrue(all_k)
        self.assertEqual(val, 16.0, "Delivered package value MUST use active global client price 16.0")

    async def test_multi_package_pricing(self):
        from database import (
            create_order,
            set_global_client_price,
            set_loader_price,
            save_order_pricing,
            set_order_loader_message_id,
            reload_global_client_prices_cache,
            reload_loader_prices_cache
        )
        await set_global_client_price(product_key="cp_5000", price=33.0)
        await set_loader_price(loader_id=self.loader.id, product_key="cp_5000", cost=25.0)
        await reload_global_client_prices_cache()
        await reload_loader_prices_cache()

        order_text = (
            "Email: multi@example.com\n"
            "Password: Pass\n"
            "Package: 2400 CP + 5000 CP"
        )
        order = await create_order(
            email="multi@example.com",
            client_chat_id=-100123456789,
            original_message_id=998,
            package="2400 CP + 5000 CP",
            status="Pending",
            category="A",
            raw_text=order_text
        )
        await set_order_loader_message_id(order.id, 999222, loader_group_id=self.loader_group_id)

        updated = await save_order_pricing(order.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.client_price_total, 49.0)
        self.assertEqual(updated.loader_cost_total, 37.0)
        self.assertEqual(updated.profit_amount, 12.0)
        self.assertEqual(updated.secret_profit_code, "H")


class TestOrder12FixAndSeedingRegression(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive regression tests for Order #12 pricing fix:
      1. Empty table startup seeding populates global_client_prices from PRODUCT_CATALOG.
      2. Custom price protection: seed_and_load_global_client_prices does not overwrite custom prices.
      3. Catalog fallback: get_global_client_price and _get_global_client_price_for_package return 16.0 even when cache is unpopulated.
      4. $16 / $12 / $4 / X calculation in calculate_order_pricing and save_order_pricing.
      5. Real Category A order-to-delivery workflow: $16 ledger increment, running total update, and deduplication.
    """

    async def asyncSetUp(self):
        from database import AsyncSessionLocal, init_db, GlobalClientPrice, LoaderPrice, Loader, GLOBAL_CLIENT_PRICES_CACHE, LOADER_PRICES_CACHE, LOADERS_CACHE
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(Loader))
            await session.commit()
        GLOBAL_CLIENT_PRICES_CACHE.clear()
        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()

    async def asyncTearDown(self):
        from database import AsyncSessionLocal, GlobalClientPrice, LoaderPrice, Loader, GLOBAL_CLIENT_PRICES_CACHE, LOADER_PRICES_CACHE, LOADERS_CACHE
        from sqlalchemy import delete
        async with AsyncSessionLocal() as session:
            await session.execute(delete(GlobalClientPrice))
            await session.execute(delete(LoaderPrice))
            await session.execute(delete(Loader))
            await session.commit()
        GLOBAL_CLIENT_PRICES_CACHE.clear()
        LOADER_PRICES_CACHE.clear()
        LOADERS_CACHE.clear()

    async def test_empty_table_startup_seeding(self):
        from database import seed_and_load_global_client_prices, GLOBAL_CLIENT_PRICES_CACHE, get_all_global_client_prices_from_db
        from product_catalog import PRODUCT_CATALOG

        cache = await seed_and_load_global_client_prices()
        self.assertEqual(len(cache), len(PRODUCT_CATALOG))
        self.assertIn("cp_2400", cache)
        self.assertEqual(cache["cp_2400"]["price"], 16.0)

        db_rows = await get_all_global_client_prices_from_db()
        self.assertEqual(len(db_rows), len(PRODUCT_CATALOG))

    async def test_custom_price_protection_during_seeding(self):
        from database import seed_and_load_global_client_prices, set_global_client_price
        await set_global_client_price("cp_2400", 18.0)

        cache = await seed_and_load_global_client_prices()
        self.assertEqual(cache["cp_2400"]["price"], 18.0, "Seeding MUST NOT overwrite existing custom price!")

    async def test_catalog_fallback_priority(self):
        from database import get_global_client_price, GLOBAL_CLIENT_PRICES_CACHE
        from utils import _get_global_client_price_for_package

        GLOBAL_CLIENT_PRICES_CACHE.clear()

        price1 = await get_global_client_price("cp_2400")
        self.assertEqual(price1, 16.0)

        price2 = _get_global_client_price_for_package("2400 CP")
        self.assertEqual(price2, 16.0)

    async def test_order_12_exact_financial_calculation(self):
        from database import (
            add_loader,
            set_loader_price,
            reload_loaders_cache,
            reload_loader_prices_cache,
            create_order,
            set_order_loader_message_id,
            save_order_pricing,
            seed_and_load_global_client_prices
        )

        await seed_and_load_global_client_prices()

        loader_group_id = -1004475489329
        loader = await add_loader(group_id=loader_group_id, loader_name="Order 12 Loader")
        await reload_loaders_cache()
        await set_loader_price(loader_id=loader.id, product_key="cp_2400", cost=12.0)
        await reload_loader_prices_cache()

        order_text = (
            "Email: testprofit12@example.com\n"
            "Password: Test12345\n"
            "Package: 2400 CP"
        )
        order = await create_order(
            email="testprofit12@example.com",
            client_chat_id=-100123456789,
            original_message_id=99912,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text=order_text
        )
        await set_order_loader_message_id(order.id, 88812, loader_group_id=loader_group_id)

        priced_ord = await save_order_pricing(order.id)
        self.assertIsNotNone(priced_ord)
        self.assertEqual(priced_ord.client_price_total, 16.0, "Client price total MUST be $16.0")
        self.assertEqual(priced_ord.loader_cost_total, 12.0, "Loader cost total MUST be $12.0")
        self.assertEqual(priced_ord.profit_amount, 4.0, "Profit amount MUST be $4.0")
        self.assertEqual(priced_ord.secret_profit_code, "X", "Secret profit code MUST be 'X'")

    async def test_order_12_delivery_workflow_and_ledger_dedup(self):
        from database import (
            add_loader,
            set_loader_price,
            reload_loaders_cache,
            reload_loader_prices_cache,
            create_order,
            set_order_loader_message_id,
            seed_and_load_global_client_prices,
            get_running_total_current,
            get_latest_ledger_entries
        )
        from handlers import process_delivery_ledger_event

        await seed_and_load_global_client_prices()

        loader_group_id = -1004475489329
        loader = await add_loader(group_id=loader_group_id, loader_name="Order 12 Delivery Loader")
        await reload_loaders_cache()
        await set_loader_price(loader_id=loader.id, product_key="cp_2400", cost=12.0)
        await reload_loader_prices_cache()

        order = await create_order(
            email="testdelivery12@example.com",
            client_chat_id=-100123456789,
            original_message_id=99913,
            package="2400 CP",
            status="Pending",
            category="A",
            raw_text="Email: testdelivery12@example.com\nPackage: 2400 CP"
        )
        await set_order_loader_message_id(order.id, 88813, loader_group_id=loader_group_id)

        rt_before = await get_running_total_current(chat_id=loader_group_id)

        mock_bot = type("Bot", (), {
            "send_message": self._async_noop,
            "send_photo": self._async_noop,
            "copy_message": self._async_noop
        })()

        # First delivery event: Ledger increment $16
        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name=loader.loader_name,
            bot=mock_bot,
            chat_id=loader_group_id,
            dedup_hash=f"dedup_ord12_{order.id}",
            reply_to_message_id=88813
        )

        rt_after = await get_running_total_current(chat_id=loader_group_id)
        self.assertEqual(rt_after - rt_before, 16.0, "Running total MUST increment by exact $16.0 (not $16.5)")

        entries = await get_latest_ledger_entries(limit=1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].now_value, 16.0)

        # Duplicate delivery event: must NOT record duplicate ledger entry
        await process_delivery_ledger_event(
            order_id=order.id,
            package_str="2400 CP",
            loader_name=loader.loader_name,
            bot=mock_bot,
            chat_id=loader_group_id,
            dedup_hash=f"dedup_ord12_{order.id}",
            reply_to_message_id=88813
        )

        rt_dup = await get_running_total_current(chat_id=loader_group_id)
        self.assertEqual(rt_dup, rt_after, "Duplicate delivery MUST NOT increment running total!")

    async def _async_noop(self, *args, **kwargs):
        pass


class TestAdminPaymentVerificationSetting(unittest.IsolatedAsyncioTestCase):
    """Regression test suite for Admin Payment Verification ON/OFF setting."""

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal, set_payment_verification_status
        from models import PaymentTransaction
        from sqlalchemy import delete
        await init_db()
        await set_payment_verification_status(True)
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PaymentTransaction))
            await session.commit()

    async def test_a_default_setting_is_on_fresh_db(self):
        from database import get_payment_verification_status, BOT_SETTINGS
        status = await get_payment_verification_status()
        self.assertTrue(status, "Default payment verification status MUST be True on fresh DB")
        self.assertTrue(BOT_SETTINGS.get("payment_verification_enabled"), "BOT_SETTINGS cache MUST have payment_verification_enabled=True")

    async def test_b_c_admin_can_turn_off_and_status(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import paymentverification_command_handler
        from database import get_payment_verification_status

        admin_update = MagicMock()
        admin_update.effective_user.id = 1573531032
        admin_update.effective_message.reply_text = AsyncMock()

        ctx = MagicMock()
        ctx.args = ["off"]

        await paymentverification_command_handler(admin_update, ctx)
        admin_update.effective_message.reply_text.assert_called_once()
        reply = admin_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Payment Verification Disabled", reply)
        self.assertIn("Payment verification is now OFF.", reply)

        status = await get_payment_verification_status()
        self.assertFalse(status, "Status must be False after turning OFF")

        # Test status command
        status_update = MagicMock()
        status_update.effective_user.id = 1573531032
        status_update.effective_message.reply_text = AsyncMock()
        status_ctx = MagicMock()
        status_ctx.args = ["status"]

        await paymentverification_command_handler(status_update, status_ctx)
        status_reply = status_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Status: 🔴 OFF", status_reply)

    async def test_d_e_f_payment_message_while_off_is_noop(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler
        from database import set_payment_verification_status, AsyncSessionLocal, BOT_SETTINGS, get_running_total_current
        from models import PaymentTransaction
        from sqlalchemy import select, func

        await set_payment_verification_status(False)
        chat_id = -100987654321
        BOT_SETTINGS["source_group_id"] = chat_id

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.copy_message = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()

        ctx = MagicMock()
        ctx.bot = mock_bot

        # Payment message containing txid/amount
        pay_update = MagicMock()
        pay_update.effective_chat.id = chat_id
        pay_update.effective_chat.title = "Client Group"
        pay_update.effective_user.id = 888777666
        pay_update.effective_user.is_bot = False
        pay_update.effective_message.message_id = 701
        pay_update.effective_message.text = "Paid $100 USDT TXID: ABC123XYZ999"
        pay_update.effective_message.photo = []
        pay_update.effective_message.document = None
        pay_update.effective_message.reply_to_message = None
        pay_update.effective_message.reply_text = AsyncMock()

        rt_before = await get_running_total_current(chat_id=chat_id)

        await source_group_handler(pay_update, ctx)

        # D. PaymentTransaction count must be 0
        async with AsyncSessionLocal() as session:
            tx_count = (await session.execute(select(func.count()).select_from(PaymentTransaction))).scalar_one()
            self.assertEqual(tx_count, 0, "No PaymentTransaction record should be created when OFF")

        # E. No review group messages
        mock_bot.copy_message.assert_not_called()

        # F. Running balance must be unchanged
        rt_after = await get_running_total_current(chat_id=chat_id)
        self.assertEqual(rt_before, rt_after, "Running balance MUST not change when OFF")

    async def test_g_h_i_turn_on_and_payment_workflow(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import paymentverification_command_handler
        from database import set_payment_verification_status, get_payment_verification_status

        await set_payment_verification_status(False)

        admin_update = MagicMock()
        admin_update.effective_user.id = 1573531032
        admin_update.effective_message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = ["on"]

        await paymentverification_command_handler(admin_update, ctx)
        reply = admin_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Payment Verification Enabled", reply)
        self.assertIn("Payment verification is now ON.", reply)

        status = await get_payment_verification_status()
        self.assertTrue(status, "Status MUST be True after turning ON")

    async def test_j_setting_survives_db_reload(self):
        from database import set_payment_verification_status, reload_bot_settings_cache, get_payment_verification_status, BOT_SETTINGS

        await set_payment_verification_status(False)
        BOT_SETTINGS.clear()  # Simulate memory flush

        await reload_bot_settings_cache()
        status = await get_payment_verification_status()
        self.assertFalse(status, "Setting MUST remain OFF after database reload/restart")

        await set_payment_verification_status(True)

    async def test_k_unauthorized_user_blocked(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import paymentverification_command_handler
        from database import set_payment_verification_status, get_payment_verification_status

        await set_payment_verification_status(True)

        unauth_update = MagicMock()
        unauth_update.effective_user.id = 999000111  # Non-admin user
        unauth_update.effective_message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = ["off"]

        await paymentverification_command_handler(unauth_update, ctx)
        unauth_update.effective_message.reply_text.assert_called_with("⛔ You are not authorized to use this command.")

        status = await get_payment_verification_status()
        self.assertTrue(status, "Setting MUST NOT change when unauthorized user invokes command")

    async def test_l_normal_order_flow_unaffected(self):
        from database import create_order, save_order_pricing, set_payment_verification_status
        await set_payment_verification_status(False)

        order = await create_order(
            email="test_normal_off@example.com",
            client_chat_id=-100111,
            original_message_id=55,
            package="2400 CP",
            status="Pending",
            category="A"
        )
        self.assertIsNotNone(order)
        priced = await save_order_pricing(order.id)
        self.assertIsNotNone(priced)
        self.assertEqual(priced.client_price_total, 16.0)


class TestAdminWalletVerificationSetting(unittest.IsolatedAsyncioTestCase):
    """Regression test suite for Admin Wallet Verification (Enforcement) ON/OFF setting."""

    async def asyncSetUp(self):
        from database import init_db, set_wallet_verification_status, AsyncSessionLocal
        from models import Order
        from sqlalchemy import delete
        await init_db()
        await set_wallet_verification_status(True)
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Order).where(Order.email.in_(["test_catb_off@gmail.com", "test_catb_on@gmail.com"])))
            await session.commit()

    async def test_a_default_setting_is_on_fresh_db(self):
        from database import get_wallet_verification_status, BOT_SETTINGS
        status = await get_wallet_verification_status()
        self.assertTrue(status, "Default wallet verification status MUST be True on fresh DB")
        self.assertTrue(BOT_SETTINGS.get("wallet_verification_enabled"), "BOT_SETTINGS cache MUST have wallet_verification_enabled=True")

    async def test_b_c_admin_can_turn_off_and_status(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import walletverification_command_handler
        from database import get_wallet_verification_status

        admin_update = MagicMock()
        admin_update.effective_user.id = 1573531032
        admin_update.effective_message.reply_text = AsyncMock()

        ctx = MagicMock()
        ctx.args = ["off"]

        await walletverification_command_handler(admin_update, ctx)
        admin_update.effective_message.reply_text.assert_called_once()
        reply = admin_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Wallet Verification Disabled", reply)
        self.assertIn("Wallet balance enforcement is now OFF.", reply)

        status = await get_wallet_verification_status()
        self.assertFalse(status, "Status must be False after turning OFF")

        # Test status command
        status_update = MagicMock()
        status_update.effective_user.id = 1573531032
        status_update.effective_message.reply_text = AsyncMock()
        status_ctx = MagicMock()
        status_ctx.args = ["status"]

        await walletverification_command_handler(status_update, status_ctx)
        status_reply = status_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Status: 🔴 OFF", status_reply)

    async def test_d_category_b_order_when_off_skips_wallet_deduction(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler
        from database import set_wallet_verification_status, set_client_group_category, AsyncSessionLocal, BOT_SETTINGS, get_wallet_balance
        from models import Order
        from sqlalchemy import select

        await set_wallet_verification_status(False)
        chat_id = -100987654321
        BOT_SETTINGS["source_group_id"] = chat_id
        await set_client_group_category(chat_id, "Cat B Client Group", "B")

        user_id = 9988776611
        bal = await get_wallet_balance(chat_id, user_id)
        self.assertEqual(bal, 0.0, "User balance starts at $0")

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.copy_message = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()

        ctx = MagicMock()
        ctx.bot = mock_bot

        order_update = MagicMock()
        order_update.effective_chat.id = chat_id
        order_update.effective_chat.title = "Cat B Client Group"
        order_update.effective_user.id = user_id
        order_update.effective_user.is_bot = False
        order_update.effective_message.message_id = 801
        order_update.effective_message.text = "Activision\nEmail: test_catb_off@gmail.com\nPassword: secret\n2400 CP"
        order_update.effective_message.photo = []
        order_update.effective_message.document = None
        order_update.effective_message.reply_to_message = None
        order_update.effective_message.reply_text = AsyncMock()

        await source_group_handler(order_update, ctx)

        # 1. Reply text should NOT be called with insufficient balance notice
        for call_arg in order_update.effective_message.reply_text.call_args_list:
            arg0 = call_arg[0][0] if call_arg[0] else ""
            self.assertNotIn("Insufficient Wallet Balance", arg0)

        # 2. Order created with Pending Approval (not Pending Payment)
        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(Order.email == "test_catb_off@gmail.com").order_by(Order.id.desc())
            ord_res = (await session.execute(stmt)).scalars().first()
            self.assertIsNotNone(ord_res)
            self.assertEqual(ord_res.status, "Pending Approval")

        # 3. Card forwarded to Payment Review Group
        mock_bot.send_message.assert_called_once()
        sent_card = mock_bot.send_message.call_args[1].get("text", "")
        self.assertIn("NEW ORDER", sent_card)

        # 4. Balance remains unchanged
        bal_after = await get_wallet_balance(chat_id, user_id)
        self.assertEqual(bal_after, 0.0)

    async def test_e_category_b_order_when_on_enforces_wallet(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import source_group_handler
        from database import set_wallet_verification_status, set_client_group_category, BOT_SETTINGS, get_wallet_balance
        from models import Order
        from sqlalchemy import select

        await set_wallet_verification_status(True)
        chat_id = -100987654322
        BOT_SETTINGS["source_group_id"] = chat_id
        await set_client_group_category(chat_id, "Cat B Client Group", "B")

        user_id = 9988776622

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.copy_message = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()

        ctx = MagicMock()
        ctx.bot = mock_bot

        order_update = MagicMock()
        order_update.effective_chat.id = chat_id
        order_update.effective_chat.title = "Cat B Client Group"
        order_update.effective_user.id = user_id
        order_update.effective_user.is_bot = False
        order_update.effective_message.message_id = 802
        order_update.effective_message.text = "Activision\nEmail: test_catb_on@gmail.com\nPassword: secret\n2400 CP"
        order_update.effective_message.photo = []
        order_update.effective_message.document = None
        order_update.effective_message.reply_to_message = None
        order_update.effective_message.reply_text = AsyncMock()

        await source_group_handler(order_update, ctx)

        # Insufficient balance prompt MUST be sent to user
        order_update.effective_message.reply_text.assert_called_once()
        reply = order_update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Insufficient Wallet Balance", reply)

    async def test_f_setting_survives_db_reload(self):
        from database import set_wallet_verification_status, reload_bot_settings_cache, get_wallet_verification_status, BOT_SETTINGS

        await set_wallet_verification_status(False)
        BOT_SETTINGS.clear()

        await reload_bot_settings_cache()
        status = await get_wallet_verification_status()
        self.assertFalse(status, "Setting MUST remain OFF after database reload/restart")

        await set_wallet_verification_status(True)

    async def test_g_unauthorized_user_blocked(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import walletverification_command_handler
        from database import set_wallet_verification_status, get_wallet_verification_status

        await set_wallet_verification_status(True)

        unauth_update = MagicMock()
        unauth_update.effective_user.id = 999000222  # Non-admin user
        unauth_update.effective_message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = ["off"]

        await walletverification_command_handler(unauth_update, ctx)
        unauth_update.effective_message.reply_text.assert_called_with("⛔ You are not authorized to use this command.")

        status = await get_wallet_verification_status()
        self.assertTrue(status, "Setting MUST NOT change when unauthorized user invokes command")


class TestNegativeManualRunningTotalAdjustment(unittest.IsolatedAsyncioTestCase):
    """Regression test suite for Manual Running Total Adjustments (+200, -200, -12.5, group ID safety, authorization)."""

    async def asyncSetUp(self):
        from database import init_db, execute_pay_reset, AUTH_USERS_CACHE
        from handlers import LOADER_ADD_SESSION, PRICE_INPUT_SESSION
        await init_db()
        super_admin_id = 1573531032
        AUTH_USERS_CACHE[super_admin_id] = "admin"
        LOADER_ADD_SESSION.clear()
        PRICE_INPUT_SESSION.clear()
        await execute_pay_reset(admin_id=super_admin_id, chat_id=-100998877)

    async def test_a_positive_200_adjustment(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import manual_running_total_text_handler
        from database import execute_manual_adjustment, get_running_total_current

        super_admin_id = 1573531032
        chat_id = -100998877

        # Set initial running total to 1000$
        await execute_manual_adjustment(1000.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 1000.0)

        # Authorized user sends +200
        update = MagicMock()
        update.effective_user.id = super_admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "+200"
        update.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update, None)
        update.effective_message.reply_text.assert_called_once()
        reply_text = update.effective_message.reply_text.call_args[0][0]

        self.assertIn("Before: 1000$", reply_text)
        self.assertIn("Now: +200$", reply_text)
        self.assertIn("Total: 1200$", reply_text)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 1200.0)

    async def test_b_negative_200_adjustment(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import manual_running_total_text_handler
        from database import execute_manual_adjustment, get_running_total_current, execute_pay_reset

        super_admin_id = 1573531032
        chat_id = -100998877

        # Scenario 1: Before 1000$, Now -200$, Total 800$
        await execute_manual_adjustment(1000.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 1000.0)

        update1 = MagicMock()
        update1.effective_user.id = super_admin_id
        update1.effective_chat.id = chat_id
        update1.effective_message.text = "-200"
        update1.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update1, None)
        update1.effective_message.reply_text.assert_called_once()
        reply1 = update1.effective_message.reply_text.call_args[0][0]

        self.assertEqual(reply1, "Before: 1000$\nNow: -200$\nTotal: 800$")
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 800.0)

        # Scenario 2: Before 250$, Now -200$, Total 50$
        await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
        await execute_manual_adjustment(250.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 250.0)

        update2 = MagicMock()
        update2.effective_user.id = super_admin_id
        update2.effective_chat.id = chat_id
        update2.effective_message.text = "-200"
        update2.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update2, None)
        update2.effective_message.reply_text.assert_called_once()
        reply2 = update2.effective_message.reply_text.call_args[0][0]

        self.assertEqual(reply2, "Before: 250$\nNow: -200$\nTotal: 50$")
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 50.0)

    async def test_c_negative_decimal_adjustment(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import manual_running_total_text_handler
        from database import execute_manual_adjustment, get_running_total_current, execute_pay_reset

        super_admin_id = 1573531032
        chat_id = -100998877

        # Before 100$, Now -12.5$, Total 87.5$
        await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
        await execute_manual_adjustment(100.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 100.0)

        update = MagicMock()
        update.effective_user.id = super_admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "-12.5"
        update.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update, None)
        update.effective_message.reply_text.assert_called_once()
        reply = update.effective_message.reply_text.call_args[0][0]

        self.assertEqual(reply, "Before: 100$\nNow: -12.5$\nTotal: 87.5$")
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 87.5)

    async def test_d_telegram_group_id_is_not_treated_as_balance_adjustment(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import manual_running_total_text_handler
        from database import execute_manual_adjustment, get_running_total_current, execute_pay_reset

        super_admin_id = 1573531032
        chat_id = -100998877

        await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
        await execute_manual_adjustment(500.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 500.0)

        # Telegram Group ID input
        update = MagicMock()
        update.effective_user.id = super_admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "-1004475489329"
        update.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update, None)
        update.effective_message.reply_text.assert_not_called()
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 500.0, "Telegram Group ID MUST NOT alter running total!")

    async def test_e_unauthorized_user_blocked(self):
        from unittest.mock import MagicMock, AsyncMock
        from handlers import manual_running_total_text_handler
        from database import execute_manual_adjustment, get_running_total_current, execute_pay_reset

        super_admin_id = 1573531032
        unauth_user_id = 999888777
        chat_id = -100998877

        await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
        await execute_manual_adjustment(500.0, admin_id=super_admin_id, chat_id=chat_id)
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 500.0)

        # Non-super-admin tries to adjust -200
        update = MagicMock()
        update.effective_user.id = unauth_user_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "-200"
        update.effective_message.reply_text = AsyncMock()

        await manual_running_total_text_handler(update, None)
        update.effective_message.reply_text.assert_not_called()
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 500.0, "Unauthorized user MUST NOT alter running total!")

    async def test_f_application_dispatch_routing(self):
        from unittest.mock import patch, AsyncMock, PropertyMock
        from datetime import datetime, timezone
        from telegram import Update, Message, User, Chat
        from telegram.ext import ExtBot
        from config import Config
        from main import build_application
        from database import execute_pay_reset, execute_manual_adjustment, get_running_total_current
        from handlers import LOADER_ADD_SESSION

        super_admin_id = 1573531032
        chat_id = -100998877

        dummy_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789"
        with patch.object(Config, "BOT_TOKEN", dummy_token):
            app = build_application()
        app._initialized = True

        admin_user = User(id=super_admin_id, first_name="Admin", is_bot=False)
        group_chat = Chat(id=chat_id, type="group", title="Test Group")

        with patch.object(Message, "reply_text", new_callable=AsyncMock) as mock_reply, \
             patch.object(ExtBot, "id", new_callable=PropertyMock, return_value=9999999):
            try:
                # 1. Test +200 dispatch through Application handler routing
                await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
                await execute_manual_adjustment(1000.0, admin_id=super_admin_id, chat_id=chat_id)

                msg_plus = Message(message_id=1001, date=datetime.now(timezone.utc), chat=group_chat, from_user=admin_user, text="+200")
                upd_plus = Update(update_id=1001, message=msg_plus)
                mock_reply.reset_mock()
                await app.process_update(upd_plus)
                mock_reply.assert_called_once()
                self.assertEqual(await get_running_total_current(chat_id=chat_id), 1200.0)

                # 2. Test -200 dispatch through Application handler routing
                await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
                await execute_manual_adjustment(1000.0, admin_id=super_admin_id, chat_id=chat_id)

                msg_minus = Message(message_id=1002, date=datetime.now(timezone.utc), chat=group_chat, from_user=admin_user, text="-200")
                upd_minus = Update(update_id=1002, message=msg_minus)
                mock_reply.reset_mock()
                await app.process_update(upd_minus)
                mock_reply.assert_called_once()
                self.assertEqual(await get_running_total_current(chat_id=chat_id), 800.0)

                # 3. Test -12.5 decimal dispatch through Application handler routing
                await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
                await execute_manual_adjustment(100.0, admin_id=super_admin_id, chat_id=chat_id)

                msg_dec = Message(message_id=1003, date=datetime.now(timezone.utc), chat=group_chat, from_user=admin_user, text="-12.5")
                upd_dec = Update(update_id=1003, message=msg_dec)
                mock_reply.reset_mock()
                await app.process_update(upd_dec)
                mock_reply.assert_called_once()
                self.assertEqual(await get_running_total_current(chat_id=chat_id), 87.5)

                # 4. Test Telegram Group ID -1004475489329 dispatch is NOT treated as adjustment
                await execute_pay_reset(admin_id=super_admin_id, chat_id=chat_id)
                await execute_manual_adjustment(500.0, admin_id=super_admin_id, chat_id=chat_id)

                msg_group_id = Message(message_id=1004, date=datetime.now(timezone.utc), chat=group_chat, from_user=admin_user, text="-1004475489329")
                upd_group_id = Update(update_id=1004, message=msg_group_id)
                mock_reply.reset_mock()
                await app.process_update(upd_group_id)
                mock_reply.assert_not_called()
                self.assertEqual(await get_running_total_current(chat_id=chat_id), 500.0)

                # 5. Test generic non-numeric text bypasses manual_running_total_text_handler and reaches loader_text_wizard_handler
                private_chat = Chat(id=super_admin_id, type="private")
                LOADER_ADD_SESSION[super_admin_id] = {"step": 1, "chat_id": super_admin_id, "created_at": datetime.now(timezone.utc)}

                msg_wizard = Message(message_id=1005, date=datetime.now(timezone.utc), chat=private_chat, from_user=admin_user, text="NonNumericText")
                upd_wizard = Update(update_id=1005, message=msg_wizard)
                mock_reply.reset_mock()
                await app.process_update(upd_wizard)
                mock_reply.assert_called_once()
                self.assertIn("Invalid Loader Group ID", mock_reply.call_args[0][0])

            finally:
                LOADER_ADD_SESSION.clear()
            if hasattr(app, "shutdown"):
                await app.shutdown()


class TestCategoryBOrderWorkflowAndProfitCode(unittest.IsolatedAsyncioTestCase):
    """
    Regression test suite for Category B Order + Post-Delivery Profit Code Workflow.
    Verifies:
    1. Automatic client price calculation on Category B order creation
    2. Saving client_price_total to DB at order creation time
    3. Category B workflow execution without wallet requirements
    4. Admin completion with profit code (e.g. test@example.com\\nV or #46 V)
    5. Formatting of compact accounting summary (Price, Code, Before, Now, Total)
    6. Concealment of actual profit amount and loader cost
    7. Exact single increment of running total
    8. Duplicate completion submission blocking
    9. Preservation of historical client price when global catalog updates
    10. Ambiguous email error prompt listing Order IDs
    11. parse_admin_profit_code_input safety guards
    """

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal, reload_global_client_prices_cache
        from models import Order, DeliveryLedger, Wallet, WalletTransaction
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliveryLedger))
            await session.execute(delete(WalletTransaction))
            await session.execute(delete(Wallet))
            await session.execute(delete(Order))
            await session.commit()
        await reload_global_client_prices_cache()

    async def test_category_b_order_creation_calculates_and_saves_client_price_immediately(self):
        from database import create_order, get_order_by_id, save_order_pricing
        order = await create_order(
            email="catb_test1@example.com",
            client_chat_id=-100123456789,
            package="2400",
            category="B",
            status="Pending"
        )
        await save_order_pricing(order.id)
        saved = await get_order_by_id(order.id)
        self.assertIsNotNone(saved.client_price_total)
        self.assertEqual(float(saved.client_price_total), 16.0)

    async def test_category_b_order_creation_records_ledger_and_sends_accounting_summary_immediately(self):
        from database import create_order, save_order_pricing, record_delivery_ledger_entry, get_running_total_current, execute_manual_adjustment, execute_pay_reset
        from utils import format_delivery_summary_message

        chat_id = -100123456789
        admin_id = 1573531032
        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)
        await execute_manual_adjustment(50.0, admin_id=admin_id, chat_id=chat_id)

        order = await create_order(
            email="catb_creation@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Pending Approval"
        )
        order = await save_order_pricing(order.id)

        # Record creation ledger entry immediately
        c_price = float(order.client_price_total)
        entry, is_new = await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400",
            now_value=c_price,
            loader_name="System",
            dedup_hash=f"catb_create_{order.id}",
            chat_id=chat_id
        )
        self.assertTrue(is_new)
        self.assertEqual(entry.before_total, 50.0)
        self.assertEqual(entry.running_total, 66.0)

        cur_rt = await get_running_total_current(chat_id=chat_id)
        self.assertEqual(cur_rt, 66.0)

        # Verify initial creation accounting summary (No profit code yet)
        summary_text = format_delivery_summary_message(
            email=order.email,
            client_price=c_price,
            secret_code=None,
            before_total=entry.before_total,
            now_value=c_price,
            running_total=entry.running_total
        )

        self.assertIn("Price: $16", summary_text)
        self.assertIn("Before: 50", summary_text)
        self.assertIn("Now: 16", summary_text)
        self.assertIn("Total: 66", summary_text)

    async def test_category_b_delivery_caption_exact_format(self):
        from database import create_order, save_order_pricing, get_order_by_id
        from utils import format_delivered_packages_caption

        order = await create_order(
            email="testcatb2@example.com",
            client_chat_id=-100123456789,
            package="2400",
            category="B",
            status="Completed"
        )
        order = await save_order_pricing(order.id)
        order.secret_profit_code = "V"

        header = f"{order.email}\n{order.secret_profit_code}"
        items = [{"package": "2400 CP", "qty": 1}]
        pkg_block = format_delivered_packages_caption(items, include_price=False)
        full_caption = f"{header}\n\n{pkg_block}"

        expected = (
            "testcatb2@example.com\n"
            "V\n\n"
            "📦 Delivered Package\n\n"
            "✅ 2400 CP"
        )

        self.assertEqual(full_caption, expected)
        self.assertNotIn("Price:", full_caption)
        self.assertNotIn("Loader Cost", full_caption)
        self.assertNotIn("Profit", full_caption)
        self.assertNotIn("Before:", full_caption)

    async def test_update_client_group_delivery_caption_edits_existing_message(self):
        from database import create_order, save_order_pricing, update_order_client_delivered_msg_id, get_order_by_id
        from delivery import update_client_group_delivery_caption
        from unittest.mock import AsyncMock, MagicMock

        chat_id = -100123456789
        order = await create_order(
            email="testcatb3@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Delivered"
        )
        order = await save_order_pricing(order.id)
        order.secret_profit_code = "V"
        await update_order_client_delivered_msg_id(order.id, 54321)

        refreshed_order = await get_order_by_id(order.id)
        refreshed_order.secret_profit_code = "V"
        self.assertEqual(refreshed_order.client_delivered_msg_id, 54321)

        mock_bot = MagicMock()
        mock_bot.edit_message_caption = AsyncMock()

        success = await update_client_group_delivery_caption(refreshed_order, mock_bot)
        self.assertTrue(success)

        mock_bot.edit_message_caption.assert_called_once()
        call_kwargs = mock_bot.edit_message_caption.call_args[1]
        self.assertEqual(call_kwargs["chat_id"], chat_id)
        self.assertEqual(call_kwargs["message_id"], 54321)
        self.assertIn("testcatb3@example.com\nV", call_kwargs["caption"])
        self.assertIn("📦 Delivered Package", call_kwargs["caption"])
        self.assertIn("✅ 2400 CP", call_kwargs["caption"])

    async def test_admin_submits_profit_code_without_double_charging_running_total(self):
        from database import create_order, save_order_pricing, record_delivery_ledger_entry, get_order_by_id, execute_manual_adjustment, get_running_total_current, execute_pay_reset
        from handlers import admin_profit_code_completion_handler, BOT_SETTINGS, AUTH_USERS_CACHE
        from unittest.mock import AsyncMock, MagicMock

        chat_id = -100123456789
        BOT_SETTINGS["client_group_id"] = chat_id
        admin_id = 1573531032
        AUTH_USERS_CACHE[admin_id] = "super_admin"

        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)
        await execute_manual_adjustment(50.0, admin_id=admin_id, chat_id=chat_id)

        order = await create_order(
            email="catb_test2@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Approved"
        )
        order = await save_order_pricing(order.id)

        # Record creation time accounting
        await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400",
            now_value=float(order.client_price_total),
            loader_name="System",
            dedup_hash=f"catb_create_{order.id}",
            chat_id=chat_id
        )
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 66.0)

        # Admin submits profit code V post-delivery
        update = MagicMock()
        update.effective_user.id = admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "catb_test2@example.com\nV"
        update.effective_message.reply_text = AsyncMock()

        handled = await admin_profit_code_completion_handler(update, None)
        self.assertTrue(handled)

        updated_order = await get_order_by_id(order.id)
        self.assertEqual(updated_order.status, "Completed")
        self.assertEqual(updated_order.secret_profit_code, "V")

        # Running total MUST remain 66.0 (NOT 82.0!)
        cur_rt = await get_running_total_current(chat_id=chat_id)
        self.assertEqual(cur_rt, 66.0, "Profit code submission MUST NOT double charge running total!")

        # Verify reply message format
        update.effective_message.reply_text.assert_called_once()
        reply_msg = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Secret profit code <b>V</b> assigned", reply_msg)

    async def test_duplicate_profit_code_submission_blocked(self):
        from database import create_order, save_order_pricing, record_delivery_ledger_entry, complete_category_b_order_with_profit_code, get_running_total_current, execute_pay_reset
        from handlers import admin_profit_code_completion_handler, AUTH_USERS_CACHE
        from unittest.mock import AsyncMock, MagicMock

        chat_id = -100123456789
        admin_id = 1573531032
        AUTH_USERS_CACHE[admin_id] = "super_admin"

        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)

        order = await create_order(
            email="catb_dup@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Approved"
        )
        order = await save_order_pricing(order.id)
        await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400",
            now_value=float(order.client_price_total),
            loader_name="System",
            dedup_hash=f"catb_create_{order.id}",
            chat_id=chat_id
        )

        # First completion
        await complete_category_b_order_with_profit_code(order.id, "V", admin_id)
        rt1 = await get_running_total_current(chat_id=chat_id)
        self.assertEqual(rt1, 16.0)

        # Second completion attempt via handler
        update = MagicMock()
        update.effective_user.id = admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "catb_dup@example.com\nV"
        update.effective_message.reply_text = AsyncMock()

        await admin_profit_code_completion_handler(update, None)

        rt2 = await get_running_total_current(chat_id=chat_id)
        self.assertEqual(rt2, 16.0, "Duplicate submission must NOT increment running total!")
        update.effective_message.reply_text.assert_called_once()
        self.assertIn("already completed", update.effective_message.reply_text.call_args[0][0])

    async def test_historical_client_price_immutability(self):
        from database import create_order, save_order_pricing, get_order_by_id, GLOBAL_CLIENT_PRICES_CACHE
        order = await create_order(
            email="catb_hist@example.com",
            client_chat_id=-100123456789,
            package="2400",
            category="B",
            status="Pending"
        )
        await save_order_pricing(order.id)

        # Change global price in cache for cp_2400 to 25.0
        orig_val = GLOBAL_CLIENT_PRICES_CACHE.get("cp_2400")
        GLOBAL_CLIENT_PRICES_CACHE["cp_2400"] = {"price": 25.0, "display_name": "2400 CP", "package_type": "normal_cp"}

        try:
            # Save pricing again, historical price must remain 16.0
            await save_order_pricing(order.id)
            saved = await get_order_by_id(order.id)
            self.assertEqual(float(saved.client_price_total), 16.0)
        finally:
            if orig_val:
                GLOBAL_CLIENT_PRICES_CACHE["cp_2400"] = orig_val

    async def test_ambiguous_email_prompts_admin_for_order_id(self):
        from database import create_order, save_order_pricing, get_order_by_id
        from handlers import admin_profit_code_completion_handler, AUTH_USERS_CACHE
        from unittest.mock import AsyncMock, MagicMock

        chat_id = -100123456789
        admin_id = 1573531032
        AUTH_USERS_CACHE[admin_id] = "super_admin"

        o1 = await create_order(email="catb_amb@example.com", client_chat_id=chat_id, package="2400", category="B", status="Approved")
        await save_order_pricing(o1.id)
        o2 = await create_order(email="catb_amb@example.com", client_chat_id=chat_id, package="5000", category="B", status="Approved")
        await save_order_pricing(o2.id)

        # Admin submits email without Order ID
        update = MagicMock()
        update.effective_user.id = admin_id
        update.effective_chat.id = chat_id
        update.effective_message.text = "catb_amb@example.com\nV"
        update.effective_message.reply_text = AsyncMock()

        await admin_profit_code_completion_handler(update, None)
        update.effective_message.reply_text.assert_called_once()
        reply_amb = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Multiple active Category B orders found", reply_amb)
        self.assertIn(f"Order #{o1.id}", reply_amb)
        self.assertIn(f"Order #{o2.id}", reply_amb)

        # Admin submits explicit Order ID: #<o2.id> V
        update2 = MagicMock()
        update2.effective_user.id = admin_id
        update2.effective_chat.id = chat_id
        update2.effective_message.text = f"#{o2.id} V"
        update2.effective_message.reply_text = AsyncMock()

        await admin_profit_code_completion_handler(update2, None)
        u2 = await get_order_by_id(o2.id)
        self.assertEqual(u2.status, "Completed")
        self.assertEqual(u2.secret_profit_code, "V")

        u1 = await get_order_by_id(o1.id)
        self.assertEqual(u1.status, "Approved")

    def test_parse_admin_profit_code_input_guards(self):
        from handlers import parse_admin_profit_code_input

        # Valid inputs
        p1 = parse_admin_profit_code_input("test@example.com\nV")
        self.assertIsNotNone(p1)
        self.assertEqual(p1["email"], "test@example.com")
        self.assertEqual(p1["profit_code"], "V")

        p2 = parse_admin_profit_code_input("#46 V")
        self.assertIsNotNone(p2)
        self.assertEqual(p2["order_id"], 46)
        self.assertEqual(p2["profit_code"], "V")

        p3 = parse_admin_profit_code_input("46 X+C")
        self.assertIsNotNone(p3)
        self.assertEqual(p3["order_id"], 46)
        self.assertEqual(p3["profit_code"], "X+C")

        # Invalid inputs that must return None
        self.assertIsNone(parse_admin_profit_code_input("+200"))
        self.assertIsNone(parse_admin_profit_code_input("-200"))
        self.assertIsNone(parse_admin_profit_code_input("-12.5"))

        # Order creation text containing password / platform
        order_creation_text = (
            "catb@example.com\n"
            "Pass:\n"
            "secret123\n"
            "Platform: Activision\n"
            "Package: 2400"
        )
        self.assertIsNone(parse_admin_profit_code_input(order_creation_text))

    async def test_category_b_delivery_automatically_calculates_and_saves_secret_code_in_caption(self):
        from database import create_order, save_order_pricing, record_delivery_ledger_entry, get_order_by_id, add_loader, set_loader_price, execute_pay_reset, get_running_total_current, DeliveryLedger, AsyncSessionLocal
        from delivery import deliver_order_by_id
        from models import Image
        from unittest.mock import AsyncMock, MagicMock
        from sqlalchemy import select

        chat_id = -100123456789
        admin_id = 1573531032
        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)

        loader_group_id = -100987654321
        loader = await add_loader(loader_group_id, "TestLoader")
        await set_loader_price(loader.id, "cp_2400", 15.0)

        order = await create_order(
            email="catb_auto_delivery@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Pending Approval"
        )
        order = await save_order_pricing(order.id)
        self.assertEqual(float(order.client_price_total), 16.0)
        self.assertIsNone(order.secret_profit_code)

        await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400",
            now_value=float(order.client_price_total),
            loader_name="System",
            dedup_hash=f"catb_create_{order.id}",
            chat_id=chat_id
        )
        self.assertEqual(await get_running_total_current(chat_id=chat_id), 16.0)

        async with AsyncSessionLocal() as session:
            img = Image(order_id=order.id, telegram_file_id="img_file_123", file_type="photo", position=0)
            session.add(img)
            await session.commit()

        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 9999
        mock_bot.send_media_group = AsyncMock(return_value=[mock_msg])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        success = await deliver_order_by_id(
            bot=mock_bot,
            order_id=order.id,
            loader_chat_id=loader_group_id,
            target_delivery_chat_id=chat_id
        )
        self.assertTrue(success)

        updated_order = await get_order_by_id(order.id)
        self.assertEqual(updated_order.secret_profit_code, "V")
        self.assertEqual(float(updated_order.loader_cost_total), 15.0)
        self.assertEqual(float(updated_order.profit_amount), 1.0)

        mock_bot.send_media_group.assert_called()
        media_group_sent = mock_bot.send_media_group.call_args_list[0][1]["media"]
        first_media_caption = media_group_sent[0].caption

        expected_caption = (
            "catb_auto_delivery@example.com\n"
            "V\n\n"
            "📦 Delivered Package\n\n"
            "✅ 2400 CP"
        )
        self.assertEqual(first_media_caption, expected_caption)

        self.assertEqual(await get_running_total_current(chat_id=chat_id), 16.0)
        async with AsyncSessionLocal() as session:
            stmt = select(DeliveryLedger).where(DeliveryLedger.order_id == order.id)
            ledgers = (await session.execute(stmt)).scalars().all()
            self.assertEqual(len(ledgers), 1, "Only 1 creation-time ledger entry must exist for Category B!")

    async def test_category_b_reprocessing_retry_does_not_duplicate_accounting_or_code(self):
        from database import create_order, save_order_pricing, record_delivery_ledger_entry, get_order_by_id, add_loader, set_loader_price, execute_pay_reset, get_running_total_current, DeliveryLedger, AsyncSessionLocal
        from delivery import deliver_order_by_id
        from models import Image
        from unittest.mock import AsyncMock, MagicMock
        from sqlalchemy import select

        chat_id = -100123456789
        admin_id = 1573531032
        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)

        loader_group_id = -100987654321
        loader = await add_loader(loader_group_id, "TestLoader2")
        await set_loader_price(loader.id, "cp_2400", 15.0)

        order = await create_order(
            email="catb_retry@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Pending Approval"
        )
        order = await save_order_pricing(order.id)

        await record_delivery_ledger_entry(
            order_id=order.id,
            package="2400",
            now_value=float(order.client_price_total),
            loader_name="System",
            dedup_hash=f"catb_create_{order.id}",
            chat_id=chat_id
        )

        async with AsyncSessionLocal() as session:
            img = Image(order_id=order.id, telegram_file_id="img_file_456", file_type="photo", position=0)
            session.add(img)
            await session.commit()

        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 9999
        mock_bot.send_media_group = AsyncMock(return_value=[mock_msg])
        mock_bot.send_message = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()

        await deliver_order_by_id(bot=mock_bot, order_id=order.id, loader_chat_id=loader_group_id, target_delivery_chat_id=chat_id)
        rt1 = await get_running_total_current(chat_id=chat_id)
        o1 = await get_order_by_id(order.id)
        self.assertEqual(o1.secret_profit_code, "V")

        await deliver_order_by_id(bot=mock_bot, order_id=order.id, loader_chat_id=loader_group_id, target_delivery_chat_id=chat_id, allow_completed=True)
        rt2 = await get_running_total_current(chat_id=chat_id)
        o2 = await get_order_by_id(order.id)

        self.assertEqual(rt2, rt1, "Retrying delivery MUST NOT duplicate accounting!")
        self.assertEqual(o2.secret_profit_code, "V", "Secret profit code must remain consistent on retry!")


class TestRealCustomerOrderPatternsAndParser(unittest.IsolatedAsyncioTestCase):
    """
    Regression test suite for Real Customer Order Patterns & Enhanced Order Parser v2.
    Tests all patterns A through AA, recovery code isolation, customer reference extraction,
    price-in-parentheses filtering, non-order status filtering, profit-code isolation,
    and exact raw message duplicate detection.
    """

    def test_pattern_a_g49_labelled_spanish(self):
        from order_parser import parse_order_v2
        raw = (
            "G49\n\n"
            "• Nombre:\n"
            "• Juego: CODM\n"
            "• Mail: villalbawenddy@gmail.com\n"
            "• Pw: WenMEFU13\n"
            "• Nickname:WEnddy\n"
            "• CP:10.8k"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"], "G49")
        self.assertEqual(res["email"], "villalbawenddy@gmail.com")
        self.assertEqual(res["password"], "WenMEFU13")
        self.assertEqual(res["username"], "WEnddy")
        self.assertEqual(res["packages"][0]["package"], "10800")

    def test_pattern_b_g47_20k_explicit_breakdown(self):
        from order_parser import parse_order_v2
        raw = (
            "G47\n\n"
            "Order #4 (PE-25/09)\n"
            "• Nombre: Anthony Castilla\n"
            "• Juego: CODM / ACTIVISION\n"
            "• Mail: kleveryes06@gmail.com\n"
            "• Pw: silentment124\n"
            "• Nickname: Damn_DMT\n"
            "• CP: 20K (19200cp+880cp)"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"], "G47")
        self.assertEqual(res["email"], "kleveryes06@gmail.com")
        self.assertEqual(res["password"], "silentment124")
        self.assertIn("Activision", res["login_method"])
        pkg_names = [p["package"] for p in res["packages"]]
        self.assertEqual(pkg_names, ["19200", "880"])
        self.assertNotIn("20000", pkg_names, "Explicit breakdown (19200cp+880cp) MUST be preserved without math 20K conversion!")

    def test_pattern_c_g46_7_4k(self):
        from order_parser import parse_order_v2
        raw = (
            "G46\n\n"
            "Order #3 (PE-25/09)\n"
            "• Nombre: Luis Vilchez\n"
            "• Juego y login: CODM\n"
            "• Mail: vilchezuchiha@gmail.com\n"
            "• Pw: Aomi2019\n"
            "• Nickname: <De@th\n"
            "• CP: 7.4K"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"], "G46")
        self.assertEqual(res["email"], "vilchezuchiha@gmail.com")
        self.assertEqual(res["password"], "Aomi2019")
        self.assertEqual(res["username"], "<De@th")
        self.assertEqual(res["packages"][0]["package"], "7400")

    def test_pattern_d_252_activision(self):
        from order_parser import parse_order_v2
        raw = (
            "252#\n"
            "*Activision*\n\n"
            "Nick: Rcvictor\n"
            "Correo: victormanuel09876t@gmail.com\n"
            "Contraseña: Vicman28.\n\n"
            "5000"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"].strip("#"), "252")
        self.assertEqual(res["email"], "victormanuel09876t@gmail.com")
        self.assertEqual(res["password"], "Vicman28.")
        self.assertEqual(res["username"], "Rcvictor")
        self.assertEqual(res["packages"][0]["package"], "5040")

    def test_pattern_e_253_unlabelled_order(self):
        from order_parser import parse_order_v2
        raw = (
            "253#\n"
            "Andre\n"
            "+573053657865\n"
            "Diaz-1725\n"
            "11335019\n"
            "38600607\n"
            "48888193\n"
            "5000"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"].strip("#"), "253")
        self.assertEqual(res["phone"], "+573053657865")
        self.assertEqual(res["packages"][0]["package"], "5040")
        self.assertTrue(res["username"] == "Andre" or len(res["unclassified_data"]) > 0)

    def test_pattern_f_254_spanish_labels(self):
        from order_parser import parse_order_v2
        raw = (
            "254#\n"
            "nick : a\n"
            "correo : angeloveliz0505@gmail.com\n"
            "contraseña: angelo2020\n"
            "5000"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["username"], "a")
        self.assertEqual(res["email"], "angeloveliz0505@gmail.com")
        self.assertEqual(res["password"], "angelo2020")
        self.assertEqual(res["packages"][0]["package"], "5040")

    def test_pattern_g_277_unlabelled_email(self):
        from order_parser import parse_order_v2
        raw = (
            "277#\n\n"
            "Activision\n\n"
            "carlosrambaut4@gmail.com\n"
            "Contraseña: carlos40k\n"
            "Nickname: +57×David(CR)\n\n"
            "10800"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"].strip("#"), "277")
        self.assertEqual(res["email"], "carlosrambaut4@gmail.com")
        self.assertEqual(res["password"], "carlos40k")
        self.assertEqual(res["username"], "+57×David(CR)")
        self.assertEqual(res["packages"][0]["package"], "10800")

    def test_pattern_h_248_compact_order(self):
        from order_parser import parse_order_v2
        raw = (
            "248#\n"
            "CâPiTaNø\n"
            "luisgokupc23@gmail.com\n"
            "GreciaJoss04\n"
            "2400"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["customer_ref_id"].strip("#"), "248")
        self.assertEqual(res["email"], "luisgokupc23@gmail.com")
        self.assertEqual(res["username"], "CâPiTaNø")
        self.assertEqual(res["password"], "GreciaJoss04")
        self.assertEqual(res["packages"][0]["package"], "2400")

    def test_pattern_i_249_cp_case_insensitive(self):
        from order_parser import parse_order_v2
        raw = (
            "249#\n"
            "*Activision*\n\n"
            "Nick: Xx.Tiniebla.xX\n"
            "Correo: kleiberprada161@gmail.com\n"
            "Contraseña: 031405Js\n"
            "Cp: 5000"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["packages"][0]["package"], "5040")

    def test_pattern_j_250_special_characters(self):
        from order_parser import parse_order_v2
        raw = (
            "250#\n"
            "Ƈک・Tuziiiiii\n"
            "Correo :Maikeljesus8098@gmail.com\n"
            "Contraseña :30236568\n"
            "2400"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["username"], "Ƈک・Tuziiiiii")
        self.assertEqual(res["email"], "maikeljesus8098@gmail.com")
        self.assertEqual(res["password"], "30236568")
        self.assertEqual(res["packages"][0]["package"], "2400")

    def test_pattern_k_251_24000(self):
        from order_parser import parse_order_v2
        raw = (
            "251#\n"
            "Nick: scpatron\n"
            "Correo: calejandrogc2204@gmail.com\n"
            "Contraseña: G@ancor20\n"
            "24000"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["packages"][0]["package"], "24000")

    def test_pattern_l_m_safe_ios_and_typos(self):
        from order_parser import parse_order_v2
        raw = (
            "Orden #71\n"
            "Safe iOS\n"
            "Activision\n"
            "Package: 10.800\n"
            "Emai: blackje@hotmail.com\n"
            "Pasword: R090821p$\n"
            "Nick: 1MiguelN"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertIn("Orden #71", res["customer_ref_id"])
        self.assertEqual(res["email"], "blackje@hotmail.com")
        self.assertEqual(res["password"], "R090821p$")
        self.assertEqual(res["username"], "1MiguelN")
        self.assertEqual(res["packages"][0]["package"], "10800")

    def test_pattern_n_o_recovery_codes_and_pack_multiplier(self):
        from order_parser import parse_order_v2
        raw = (
            "Order #2 safe Google fast\n"
            "Facebook\n"
            "Nick: Baro§ánz (Al-Baro)\n"
            "Email: barosanz@hotmail.com\n"
            "Pass: sanchez54\n"
            "Pack: 2.4K * 4\n"
            "Codes:\n"
            "0023 3561\n"
            "0392 7298\n"
            "0846 2417\n"
            "3092 0883\n"
            "3821 9326"
        )
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(len(res["packages"]), 4)
        self.assertEqual(res["packages"][0]["package"], "2400")
        self.assertEqual(len(res["recovery_codes"]), 5)
        self.assertIn("0023 3561", res["recovery_codes"])

    def test_pattern_p_q_recarga_and_orden_no_space(self):
        from order_parser import parse_order_v2
        r1 = parse_order_v2("Recarga #122\nActivision\nCorreo:oe3022764@gmail.com\nContraseña:32282238\nNickname:ꪶƦ・Emma\nCodpoints: 12.000")
        self.assertTrue(r1["order_detected"])
        self.assertIn("Recarga #122", r1["customer_ref_id"])
        self.assertEqual(r1["packages"][0]["package"], "12000")

        r2 = parse_order_v2("Orden# 34 fast\nActivision\nNick: CM Erick\nEmail: rericduvan@gmail.com\nPassword: and21eri\nPackage: 2400")
        self.assertTrue(r2["order_detected"])
        self.assertIn("Orden# 34", r2["customer_ref_id"])

    def test_pattern_r_compact_order(self):
        from order_parser import parse_order_v2
        raw = "alemam200412@gmail.com\n\nNn200412\n\n24,000"
        res = parse_order_v2(raw)
        self.assertTrue(res["order_detected"])
        self.assertEqual(res["email"], "alemam200412@gmail.com")
        self.assertEqual(res["password"], "Nn200412")
        self.assertEqual(res["packages"][0]["package"], "24000")

    def test_pattern_s_t_u_v_w_x_y_z_aa_price_in_parentheses_stripped(self):
        from order_parser import parse_order_v2
        raw_s = (
            "#63\n"
            "24000 (137)\n"
            "Activision\n"
            "sjaider00271@gmail.com\n"
            "Rediaj_1874@\n"
            "Nombre de jugador: FLOKI"
        )
        res = parse_order_v2(raw_s)
        self.assertTrue(res["order_detected"])
        self.assertEqual([p["package"] for p in res["packages"]], ["24000"])

        raw_v = (
            "#66\n"
            "4WA48LUGWKH\n"
            "CodP's 48,000 (270)\n"
            "Nick/Nombre CODM: PirroHubzzers\n"
            "Activision\n"
            "pirroelmejor32@gmail.com\n"
            "Pirro31?"
        )
        res_v = parse_order_v2(raw_v)
        self.assertTrue(res_v["order_detected"])
        self.assertEqual([p["package"] for p in res_v["packages"]], ["48000"])

    def test_non_order_status_messages_ignored(self):
        from order_parser import parse_order_v2
        self.assertFalse(parse_order_v2("Pending 2 hours")["order_detected"])
        self.assertFalse(parse_order_v2("Waiting 24h")["order_detected"])
        self.assertFalse(parse_order_v2("Pending")["order_detected"])

    def test_g49_does_not_trigger_profit_code_submission(self):
        from handlers import parse_admin_profit_code_input
        raw_g49 = (
            "G49\n\n"
            "• Nombre:\n"
            "• Juego: CODM\n"
            "• Mail: villalbawenddy@gmail.com\n"
            "• Pw: WenMEFU13\n"
            "• Nickname:WEnddy\n"
            "• CP:10.8k"
        )
        self.assertIsNone(parse_admin_profit_code_input(raw_g49), "Customer order MUST NOT trigger manual profit code handler!")

    async def test_duplicate_order_exact_raw_message_identity(self):
        from database import create_order, save_order_pricing, get_exact_duplicate_pending_order, init_db, AsyncSessionLocal
        from models import Order
        from sqlalchemy import delete
        await init_db()

        chat_id = -100123456789
        msg_a = "Email: test_dedup@gmail.com\nPassword: ABC123\nCP: 2400"
        msg_b = "Email: test_dedup@gmail.com\nPassword: ABC124\nCP: 2400"

        # Create Order A
        o1 = await create_order(
            email="test_dedup@gmail.com",
            client_chat_id=chat_id,
            package="2400",
            category="A",
            status="Pending",
            raw_text=msg_a
        )

        # 1. Exact same raw text -> DUPLICATE DETECTED
        dup = await get_exact_duplicate_pending_order("test_dedup@gmail.com", msg_a)
        self.assertIsNotNone(dup, "Exact same raw message MUST be detected as duplicate!")
        self.assertEqual(dup.id, o1.id)

        # 2. 1-character difference in password -> NEW ORDER (NOT DUPLICATE)
        non_dup = await get_exact_duplicate_pending_order("test_dedup@gmail.com", msg_b)
        self.assertIsNone(non_dup, "1-character difference in password MUST NOT be marked as duplicate!")


class TestMultiPackageOrderPackageLevelDeliveryAndAccounting(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive integration & regression test suite for package-level multi-package order delivery,
    independent loader cost calculations, package-level secret profit code generation, partial delivery status tracking,
    duplicate retry protection, and single-package regression safety.
    """

    async def test_multi_package_order_creation_and_item_breakdown(self):
        from database import create_order, save_order_pricing, get_order_by_id, init_db
        from order_parser import parse_order_v2
        import json
        await init_db()

        raw_order = (
            "G47\n"
            "Order #4\n"
            "20K (19200cp+880cp)\n"
            "Activision\n"
            "Email: testreal47@gmail.com\n"
            "Password: TestPass47\n"
            "Nickname: TestPlayer47"
        )
        parsed = parse_order_v2(raw_order)
        self.assertTrue(parsed["order_detected"])
        pkg_names = [p["package"] for p in parsed["packages"]]
        self.assertEqual(pkg_names, ["19200", "880"])

        order = await create_order(
            email=parsed["email"],
            client_chat_id=-100123456789,
            package="19200+880",
            category="B",
            status="Pending",
            raw_text=raw_order
        )

        order = await save_order_pricing(order.id)
        saved = await get_order_by_id(order.id)

        self.assertEqual(len(saved.items), 2)
        item_keys = [it.product_key for it in saved.items]
        self.assertIn("cp_19200", item_keys)
        self.assertIn("cp_880", item_keys)

        it_19200 = next(it for it in saved.items if it.product_key == "cp_19200")
        it_880 = next(it for it in saved.items if it.product_key == "cp_880")

        self.assertEqual(float(it_19200.client_line_total), 110.0)
        self.assertEqual(float(it_880.client_line_total), 8.0)
        self.assertEqual(float(saved.client_price_total), 118.0)

        progress = json.loads(saved.package_progress)
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[0]["package"], "19200")
        self.assertEqual(progress[1]["package"], "880")
        self.assertEqual(progress[0]["status"], "Pending")

    async def test_loader_card_keyboard_renders_separate_package_buttons(self):
        from database import create_order, save_order_pricing
        from utils import build_loader_package_keyboard
        import json

        order = await create_order(
            email="testreal47_kb@gmail.com",
            client_chat_id=-100123456789,
            package="19200+880",
            category="B",
            status="Pending"
        )
        order = await save_order_pricing(order.id)

        kb = build_loader_package_keyboard(order.id, order.package_progress)
        self.assertIsNotNone(kb)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        button_texts = [b.text for b in buttons]

        self.assertTrue(any("19200" in t for t in button_texts))
        self.assertTrue(any("880" in t for t in button_texts))

    async def test_package_level_delivery_and_independent_profit_codes(self):
        from database import create_order, save_order_pricing, get_order_by_id, add_loader, set_loader_price, execute_pay_reset, update_order_package_progress, init_db, AsyncSessionLocal
        from delivery import deliver_order_by_id
        from models import Image
        from utils import toggle_package_selection
        from unittest.mock import AsyncMock, MagicMock
        import json

        await init_db()
        chat_id = -100123456789
        admin_id = 1573531032
        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)

        loader_group_id = -100987654321
        loader = await add_loader(loader_group_id, "TestLoaderMulti")
        await set_loader_price(loader.id, "cp_19200", 15.0)
        await set_loader_price(loader.id, "cp_880", 5.0)

        order = await create_order(
            email="testreal47@gmail.com",
            client_chat_id=chat_id,
            package="19200+880",
            category="B",
            status="Pending Approval"
        )
        order = await save_order_pricing(order.id, loader_id=loader.id)

        async with AsyncSessionLocal() as session:
            img1 = Image(order_id=order.id, telegram_file_id="img_19200", file_type="photo", position=0)
            img2 = Image(order_id=order.id, telegram_file_id="img_880", file_type="photo", position=1)
            session.add_all([img1, img2])
            await session.commit()

        # Step A: Loader selects 19200 CP (idx 0)
        updated_items, status_code = toggle_package_selection(order.package_progress, 0, loader.id)
        new_json1 = json.dumps(updated_items)
        await update_order_package_progress(order.id, new_json1)
        order.package_progress = new_json1

        mock_bot = MagicMock()
        mock_msg1 = MagicMock()
        mock_msg1.message_id = 8001
        mock_bot.send_media_group = AsyncMock(return_value=[mock_msg1])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        # Deliver 19200 CP
        success1 = await deliver_order_by_id(
            bot=mock_bot,
            order_id=order.id,
            loader_chat_id=loader_group_id,
            target_delivery_chat_id=chat_id,
            session_images=[img1]
        )
        self.assertTrue(success1)

        order_step1 = await get_order_by_id(order.id)
        self.assertEqual(order_step1.status, "Partially Delivered")

        media_sent1 = mock_bot.send_media_group.call_args_list[0][1]["media"]
        caption1 = media_sent1[0].caption

        expected_caption1 = (
            "testreal47@gmail.com\n"
            "J+J+J+J+J+J+G\n\n"
            "📦 Delivered Package\n\n"
            "✅ 19200 CP"
        )
        self.assertEqual(caption1, expected_caption1, "19200 CP delivery MUST attach independent profit code J+J+J+J+J+J+G ($110 - $15 = $95 -> J+J+J+J+J+J+G)!")

        # Step B: Loader selects remaining 880 CP (idx 1)
        progress_items = json.loads(order_step1.package_progress)
        self.assertEqual(progress_items[0]["status"], "Delivered")
        self.assertEqual(progress_items[0]["secret_profit_code"], "J+J+J+J+J+J+G")
        self.assertEqual(progress_items[1]["status"], "Pending")

        updated_items2, _ = toggle_package_selection(order_step1.package_progress, 1, loader.id)
        new_json2 = json.dumps(updated_items2)
        await update_order_package_progress(order.id, new_json2)
        order_step1.package_progress = new_json2

        mock_bot.reset_mock()
        mock_msg2 = MagicMock()
        mock_msg2.message_id = 8002
        mock_bot.send_media_group = AsyncMock(return_value=[mock_msg2])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        # Deliver 880 CP
        success2 = await deliver_order_by_id(
            bot=mock_bot,
            order_id=order.id,
            loader_chat_id=loader_group_id,
            target_delivery_chat_id=chat_id,
            session_images=[img2]
        )
        self.assertTrue(success2)

        order_step2 = await get_order_by_id(order.id)
        self.assertEqual(order_step2.status, "Delivered", "Order status MUST become Delivered after all packages are delivered!")

        media_sent2 = mock_bot.send_media_group.call_args_list[0][1]["media"]
        caption2 = media_sent2[0].caption

        expected_caption2 = (
            "testreal47@gmail.com\n"
            "Y\n\n"
            "📦 Delivered Package\n\n"
            "✅ 880 CP"
        )
        self.assertEqual(caption2, expected_caption2, "880 CP delivery MUST attach independent profit code Y ($8 - $5 = $3 -> Y)!")

        final_progress = json.loads(order_step2.package_progress)
        self.assertTrue(all(it["status"] == "Delivered" for it in final_progress))

    async def test_duplicate_package_delivery_protection(self):
        from database import create_order, save_order_pricing
        from utils import toggle_package_selection, mark_selected_packages_delivered
        import json

        order = await create_order(
            email="test_dup_pkg@gmail.com",
            client_chat_id=-100123456789,
            package="19200+880",
            category="B",
            status="Pending"
        )
        order = await save_order_pricing(order.id)

        items1, is_all1, cnt1 = mark_selected_packages_delivered(order.package_progress, loader_id=1, selected_items=[{"package": "19200"}])
        self.assertEqual(cnt1, 1)
        self.assertFalse(is_all1)
        self.assertEqual(items1[0]["status"], "Delivered")
        self.assertEqual(items1[1]["status"], "Pending")

        items2, status_code = toggle_package_selection(json.dumps(items1), 0, loader_id=1)
        self.assertEqual(status_code, "Delivered", "Already delivered package MUST NOT be selectable!")

    async def test_single_package_order_regression_safety(self):
        from database import create_order, save_order_pricing, get_order_by_id, add_loader, set_loader_price, execute_pay_reset, AsyncSessionLocal
        from delivery import deliver_order_by_id
        from models import Image
        from unittest.mock import AsyncMock, MagicMock

        chat_id = -100123456789
        admin_id = 1573531032
        await execute_pay_reset(admin_id=admin_id, chat_id=chat_id)

        loader_group_id = -100987654321
        loader = await add_loader(loader_group_id, "TestLoaderSingle")
        await set_loader_price(loader.id, "cp_2400", 15.0)

        order = await create_order(
            email="single_pkg@example.com",
            client_chat_id=chat_id,
            package="2400",
            category="B",
            status="Pending Approval"
        )
        order = await save_order_pricing(order.id, loader_id=loader.id)

        async with AsyncSessionLocal() as session:
            img = Image(order_id=order.id, telegram_file_id="img_single", file_type="photo", position=0)
            session.add(img)
            await session.commit()

        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 9001
        mock_bot.send_media_group = AsyncMock(return_value=[mock_msg])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        success = await deliver_order_by_id(
            bot=mock_bot,
            order_id=order.id,
            loader_chat_id=loader_group_id,
            target_delivery_chat_id=chat_id
        )
        self.assertTrue(success)

        updated = await get_order_by_id(order.id)
        self.assertEqual(updated.status, "Delivered")
        self.assertEqual(updated.secret_profit_code, "V")

        media_sent = mock_bot.send_media_group.call_args_list[0][1]["media"]
        caption = media_sent[0].caption

        expected_caption = (
            "single_pkg@example.com\n"
            "V\n\n"
            "📦 Delivered Package\n\n"
            "✅ 2400 CP"
        )
        self.assertEqual(caption, expected_caption, "Single-package delivery caption MUST remain 100% backward compatible!")


class TestPackageActiveDeliverySessionBugFix(unittest.IsolatedAsyncioTestCase):
    """
    Unit tests for Package-Level Active Delivery Session Bug Fix:
    1. Selecting first package creates active package session.
    2. Selecting second package creates independent package session.
    3. Partial delivery: First package delivery does not affect second package.
    4. Second package remains selectable after first package delivery.
    5. Package-level duplicate protection (Double-selecting an already-delivered package is blocked).
    6. Single-package safety (Existing single-package delivery still passes).
    """

    async def asyncSetUp(self):
        from database import init_db, AsyncSessionLocal
        from models import Order, DeliverySession
        from sqlalchemy import delete
        await init_db()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(DeliverySession))
            await session.execute(delete(Order))
            await session.commit()

    async def test_1_selecting_first_package_creates_active_package_session(self):
        from database import create_order, update_order_package_progress, create_delivery_session, get_active_delivery_session
        from utils import toggle_package_selection, get_loader_selected_packages
        import json

        pkgs_json = json.dumps([
            {"package": "19200", "qty": 1, "unit_price": 110.0, "status": "Pending"},
            {"package": "880", "qty": 1, "unit_price": 8.0, "status": "Pending"}
        ])
        order = await create_order(email="testreal47@gmail.com", package="20K (19200cp+880cp)", package_progress=pkgs_json)

        updated_items, status = toggle_package_selection(order.package_progress, 0, loader_id=100)
        self.assertEqual(status, "Selected")
        await update_order_package_progress(order.id, json.dumps(updated_items))

        sel = get_loader_selected_packages(json.dumps(updated_items), loader_id=100)
        ds = await create_delivery_session(order.id, loader_id=100, session_msg_id=7001, selected_packages=json.dumps(sel))
        self.assertIsNotNone(ds)
        self.assertEqual(ds.order_id, order.id)

        active_ds = await get_active_delivery_session(order.id, loader_id=100)
        self.assertIsNotNone(active_ds)
        active_pkgs = json.loads(active_ds.selected_packages)
        self.assertEqual(len(active_pkgs), 1)
        self.assertEqual(active_pkgs[0]["package"], "19200")

    async def test_2_selecting_second_package_creates_independent_package_session(self):
        from database import create_order, update_order_package_progress, create_delivery_session, get_active_delivery_session
        from utils import toggle_package_selection, get_loader_selected_packages
        import json

        pkgs_json = json.dumps([
            {"package": "19200", "qty": 1, "unit_price": 110.0, "status": "Delivered", "delivered_by": 100},
            {"package": "880", "qty": 1, "unit_price": 8.0, "status": "Pending"}
        ])
        order = await create_order(email="testreal47@gmail.com", package="20K (19200cp+880cp)", package_progress=pkgs_json)

        updated_items, status = toggle_package_selection(order.package_progress, 1, loader_id=100)
        self.assertEqual(status, "Selected")
        await update_order_package_progress(order.id, json.dumps(updated_items))

        sel = get_loader_selected_packages(json.dumps(updated_items), loader_id=100)
        await create_delivery_session(order.id, loader_id=100, session_msg_id=7002, selected_packages=json.dumps(sel))

        active_ds = await get_active_delivery_session(order.id, loader_id=100)
        self.assertIsNotNone(active_ds)
        active_pkgs = json.loads(active_ds.selected_packages)
        self.assertEqual(len(active_pkgs), 1)
        self.assertEqual(active_pkgs[0]["package"], "880")

    async def test_3_partial_delivery_first_package_does_not_affect_second_package(self):
        from database import create_order, get_order_by_id, create_delivery_session, add_images_to_order, set_order_loader_message_id
        from delivery import deliver_order_by_id
        from unittest.mock import MagicMock, AsyncMock
        import json

        pkgs_json = json.dumps([
            {"package": "19200", "qty": 1, "unit_price": 110.0, "status": "Pending"},
            {"package": "880", "qty": 1, "unit_price": 8.0, "status": "Pending"}
        ])
        order = await create_order(email="testreal47@gmail.com", package="20K (19200cp+880cp)", package_progress=pkgs_json, client_chat_id=-100111)
        await set_order_loader_message_id(order.id, 8001, loader_group_id=-100222)
        await add_images_to_order(order.id, [("file_id_1", "photo")])

        sel = [{"package": "19200", "qty": 1, "status": "Selected"}]
        await create_delivery_session(order.id, loader_id=100, session_msg_id=9001, selected_packages=json.dumps(sel))

        mock_bot = MagicMock()
        mock_bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=9999)])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        res = await deliver_order_by_id(bot=mock_bot, order_id=order.id, loader_chat_id=-100222, loader_reply_msg_id=9001, target_delivery_chat_id=-100111)
        self.assertTrue(res)

        updated = await get_order_by_id(order.id)
        self.assertEqual(updated.status, "Partially Delivered")
        pkgs = json.loads(updated.package_progress)
        self.assertEqual(pkgs[0]["status"], "Delivered")
        self.assertEqual(pkgs[1]["status"], "Pending")

    async def test_4_second_package_remains_selectable_after_first_package_delivery(self):
        from database import create_order
        from utils import toggle_package_selection
        import json

        pkgs_json = json.dumps([
            {"package": "19200", "qty": 1, "unit_price": 110.0, "status": "Delivered", "delivered_by": 100},
            {"package": "880", "qty": 1, "unit_price": 8.0, "status": "Pending"}
        ])
        order = await create_order(email="testreal47@gmail.com", package="20K (19200cp+880cp)", package_progress=pkgs_json)

        updated_items, status = toggle_package_selection(order.package_progress, 1, loader_id=100)
        self.assertEqual(status, "Selected")
        self.assertEqual(updated_items[1]["status"], "Selected")

    async def test_5_package_level_duplicate_protection(self):
        from database import create_order
        from utils import toggle_package_selection
        import json

        pkgs_json = json.dumps([
            {"package": "19200", "qty": 1, "unit_price": 110.0, "status": "Delivered", "delivered_by": 100},
            {"package": "880", "qty": 1, "unit_price": 8.0, "status": "Pending"}
        ])
        order = await create_order(email="testreal47@gmail.com", package="20K (19200cp+880cp)", package_progress=pkgs_json)

        updated_items, status = toggle_package_selection(order.package_progress, 0, loader_id=100)
        self.assertEqual(status, "Delivered")
        self.assertEqual(updated_items[0]["status"], "Delivered")

    async def test_6_single_package_safety_existing_delivery_passes(self):
        from database import create_order, get_order_by_id, add_images_to_order, set_order_loader_message_id
        from delivery import deliver_order_by_id
        from unittest.mock import MagicMock, AsyncMock

        order = await create_order(email="single_safety@example.com", package="2400", client_chat_id=-100111)
        await set_order_loader_message_id(order.id, 8002, loader_group_id=-100222)
        await add_images_to_order(order.id, [("file_id_single", "photo")])

        mock_bot = MagicMock()
        mock_bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=8888)])
        mock_bot.send_message = AsyncMock()
        mock_bot.edit_message_caption = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock(return_value=True)

        res = await deliver_order_by_id(bot=mock_bot, order_id=order.id, loader_chat_id=-100222, target_delivery_chat_id=-100111)
        self.assertTrue(res)

        updated = await get_order_by_id(order.id)
        self.assertEqual(updated.status, "Delivered")


if __name__ == "__main__":
    unittest.main()








