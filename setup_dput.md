~/.devscripts should be:

    EMAIL="erikj@matrix.org"
    DEBRELEASE_UPLOADER=dput
    DSCVERIFY_KEYRINGS="/etc/apt/trusted.gpg:~/.gnupg/pubring.gpg"

~/.dput.cf:

    [matrix]
    login                   = packages
    method                  = scp
    fqdn                    = ldc-prd-matrix-001.openmarket.com
    incoming                = /sites/matrix/packages/debian/incoming/
    post_upload_command     = ssh packages@ldc-prd-matrix-001.openmarket.com 'cd /sites/matrix/packages/debian && reprepro -V processincoming incoming'
