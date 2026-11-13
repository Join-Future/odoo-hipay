-- Clear the HiPay keys on a neutralized (duplicated or staging) database.
UPDATE payment_provider
   SET hipay_secret_key_test = NULL,
       hipay_secret_key_live = NULL,
       hipay_webhook_secret_test = NULL,
       hipay_webhook_secret_live = NULL;
