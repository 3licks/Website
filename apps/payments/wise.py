from base64 import b64decode, b64encode
import logging

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from datetime import datetime, timedelta
from flask import abort, current_app as app, request
from munch import munchify
import pywisetransfer
from pywisetransfer.webhooks import verify_signature
import requests

from models.payment import BankAccount, BankTransaction
from . import payments
from main import db

logger = logging.getLogger(__name__)


webhook_handlers = {}


def webhook(type=None):
    def inner(f):
        webhook_handlers[type] = f
        return f

    return inner


@payments.route("/wise-webhook", methods=["POST"])
def wise_webhook():
    # Wise doesn't have the ability to resend webhooks, so log them out in case something goes wrong
    logger.info(
        "Received Wise webhook request: data=%r headers=%r",
        request.data,
        request.headers,
    )

    try:
        b64decode(request.headers["X-Signature"])
        if request.json is None:
            raise ValueError("Request does not contain JSON")
    except Exception as e:
        logger.exception("Unable to parse Wise webhook request")
        abort(400)

    valid_signature = verify_signature(
        request.data,
        request.headers["X-Signature"],
    )
    if not valid_signature:
        logger.exception("Error verifying Wise webhook signature")
        abort(400)

    schema_version = request.json.get("schema_version")
    if schema_version != "2.0.0":
        logger.warning("Unsupported Wise schema version %s", schema_version)
        abort(500)

    event_type = request.json.get("event_type")
    try:
        handler = webhook_handlers[event_type]
    except KeyError as e:
        logger.warning("Unhandled Wise webhook event type %s", event_type)
        # logger.info("Webhook data: %s", request.data)
        abort(500)

    try:
        return handler(event_type, request.json)
    except Exception as e:
        logger.exception("Unhandled exception during Wise webhook")
        # logger.info("Webhook data: %s", request.data)
        abort(500)


@webhook("balances#credit")
def wise_balance_credit(event_type, event):
    profile_id = event.get("data", {}).get("resource", {}).get("profile_id")
    if profile_id is None:
        logger.exception("Missing profile_id in Wise webhook")
        # logger.info("Webhook data: %s", request.data)
        abort(400)

    borderless_account_id = event.get("data", {}).get("resource", {}).get("id")
    if borderless_account_id is None:
        logger.exception("Missing borderless_account_id in Wise webhook")
        # logger.info("Webhook data: %s", request.data)
        abort(400)

    if borderless_account_id == 0:
        # A credit event with an account ID of 0 is sent when webhook connections are configured.
        return ("", 204)

    currency = event.get("data", {}).get("currency")
    if currency is None:
        logger.exception("Missing currency in Wise webhook")
        # logger.info("Webhook data: %s", request.data)
        abort(400)

    logger.info(
        "Checking Wise details for borderless_account_id %s and currency %s",
        borderless_account_id,
        currency,
    )
    # Find the Wise bank account in the application database
    bank_account = BankAccount.query.filter_by(
        borderless_account_id=borderless_account_id,
        currency=currency,
        active=True,
    ).first()
    if not bank_account:
        logger.warning("Could not find bank account")
        return ("", 204)

    # Retrieve an account transaction statement for the past week
    interval_end = datetime.now()
    interval_start = interval_end - timedelta(days=7)
    try:
        statement = wise_statement(
            profile_id,
            borderless_account_id,
            currency,
            interval_start.isoformat() + "Z",
            interval_end.isoformat() + "Z",
        )
    except Exception as e:
        # TODO: send an email?
        logger.exception("Could not fetch statement")
        return ("", 204)

    # Lock the bank account as BankTransactions don't have an external unique ID
    # TODO: we could include referenceNumber to prevent this or at least detect issues
    BankAccount.query.with_for_update().get(bank_account.id)

    # Retrieve or construct transactions for each credit in the statement
    txns = []
    for transaction in statement.transactions:
        if transaction.type != "CREDIT":
            continue

        # Attempt to find transaction in the application database
        # TODO: we should probably check the amount_int, too
        txn = BankTransaction.query.filter_by(
            account_id=bank_account.id,
            posted=transaction.date,
            type=transaction.details.type.lower(),
            payee=transaction.details.paymentReference,
        ).first()

        # Construct a transaction record if not found
        if txn:
            continue

        txn = BankTransaction(
            account_id=bank_account.id,
            posted=transaction.date,
            type=transaction.details.type.lower(),
            amount=transaction.amount.value,
            payee=transaction.details.paymentReference,
        )
        db.session.add(txn)
        txns.append(txn)

    logging.info("Imported %s transactions", len(txns))
    db.session.commit()

    return ("", 204)


def wise_statement(profile_id, account_id, currency, interval_start, interval_end):
    client = pywisetransfer.Client()
    domain = client.borderless_accounts.service.domain
    headers = client.borderless_accounts.service.required_headers
    params = {
        "currency": currency,
        "intervalStart": interval_start,
        "intervalEnd": interval_end,
    }
    url = f"{domain}/v3/profiles/{profile_id}/borderless-accounts/{account_id}/statement.json"
    resp = requests.get(url, params=params, headers=headers)

    if resp.status_code == 403 and resp.headers["X-2FA-Approval-Result"] == "REJECTED":
        challenge = resp.headers["X-2FA-Approval"]
        key_file = open(app.config["TRANSFERWISE_PRIVATE_KEY"], "rb")
        key = load_pem_private_key(key_file.read(), None)
        signature = key.sign(
            challenge.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        headers["X-Signature"] = b64encode(signature).decode("ascii")
        headers["X-2FA-Approval"] = challenge

        resp = requests.get(url, params=params, headers=headers)

    if resp.status_code != 200 or resp.headers["X-2FA-Approval-Result"] != "APPROVED":
        raise Exception("Error fetching statement")

    return munchify(resp.json())


def wise_business_profile():
    client = pywisetransfer.Client()

    if app.config.get("TRANSFERWISE_PROFILE_ID"):
        id = int(app.config["TRANSFERWISE_PROFILE_ID"])
        borderless_accounts = list(client.borderless_accounts.list(profile_id=id))
        if len(borderless_accounts) == 0:
            raise Exception("Provided TRANSFERWISE_PROFILE_ID has no accoutns")
    else:
        # Wise bug:
        # As of 11-2021, this endpoint only returns one random business profile.
        # So if you have multiple business profiles (as we do in production),
        # you'll need to set it manually as above.
        profiles = client.profiles.list(type="business")
        profiles = list(filter(lambda p: p.type == "business", profiles))

        if len(profiles) > 1:
            raise Exception("Multiple business profiles found")
        id = profiles[0].id
    return id


def _collect_bank_accounts(borderless_account):
    # Wise creates the concept of a multi-currency account by calling normal
    # bank accounts "balances", and collecting them into a "borderless account",
    # one balance per currency. As far as we're concerned, "balances" are bank
    # accounts, as that's what people will be sending money to.
    for account in borderless_account.balances:
        try:
            if not account.bankDetails:
                continue
            if not account.bankDetails.bankAddress:
                continue
        except AttributeError:
            continue

        address = ", ".join(
            [
                account.bankDetails.bankAddress.addressFirstLine,
                account.bankDetails.bankAddress.city
                + " "
                + (account.bankDetails.bankAddress.postCode or ""),
                account.bankDetails.bankAddress.country,
            ]
        )

        sort_code = account_number = None

        if account.bankDetails.currency == "GBP":
            # bankCode is the SWIFT code for non-GBP accounts.
            sort_code = account.bankDetails.bankCode

            if len(account.bankDetails.accountNumber) == 8:
                account_number = account.bankDetails.accountNumber
            else:
                # Wise bug:
                # accountNumber is sometimes erroneously the IBAN for GBP accounts.
                # Extract the account number from the IBAN.
                account_number = account.bankDetails.accountNumber.replace(" ", "")[-8:]

        yield BankAccount(
            sort_code=sort_code,
            acct_id=account_number,
            currency=account.bankDetails.currency,
            active=False,
            institution=account.bankDetails.bankName,
            address=address,
            swift=account.bankDetails.get("swift"),
            iban=account.bankDetails.get("iban"),
            # Webhooks only include the borderlessAccountId
            borderless_account_id=borderless_account.id,
        )


def wise_retrieve_accounts():
    business_profile = wise_business_profile()

    if not business_profile:
        return

    client = pywisetransfer.Client()
    borderless_accounts = client.borderless_accounts.list(profile_id=business_profile)

    for borderless_account in borderless_accounts:
        for bank_account in _collect_bank_accounts(borderless_account):
            yield bank_account


def wise_validate():
    """Validate that Wise is configured and operational"""
    result = []

    env = app.config.get("TRANSFERWISE_ENVIRONMENT")
    if env == "sandbox":
        result.append((True, "Sandbox environment being used"))
    elif env == "live":
        result.append((True, "Live environment being used"))
    else:
        result.append((False, "No environment configured"))
        return result

    val = app.config.get("TRANSFERWISE_API_TOKEN", "")
    if len(val) == 36:
        result.append((True, "Access token set"))
    else:
        result.append((False, "Access token not set"))
        return result

    try:
        client = pywisetransfer.Client()
        client.users.me()
        result.append((True, "Connection to Wise API succeeded"))
    except Exception as e:
        result.append((False, f"Unable to connect to Wise: {e}"))
        return result

    business_profile = wise_business_profile()
    if business_profile:
        result.append((True, "Wise business profile exists"))
    else:
        result.append((False, "Wise business profile does not exist"))

    webhooks = client.subscriptions.list(profile_id=business_profile)
    if webhooks:
        result.append((True, "Webhook event subscriptions are present"))
    else:
        result.append((False, "Webhook event subscriptions are not present"))

    return result
