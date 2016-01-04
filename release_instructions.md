You will need to have the following (non-exhaustive) packages:

    - ubuntu-dev-tools
    - git-buildbackage
    - sbuild

You should create a bunch of schroots (see mk-sbuild) and add the matrix
debian repository to all the schroots.

    mk-sbuild --eatmydata wheezy
    # Logout/Login to get a new session
    sudo schroot -c source:wheezy-amd64 -u root # Enter the schroot
    echo deb http://matrix.org/packages/debian/ wheezy main > /etc/apt/sources.list.d/matrix.list
    apt-key add - <<EOF # Copy key from https://matrix.org/packages/debian/repo-key.asc
    EOF
    apt-get update
    exit # Leave the schroot
    
You will want to set ~/.gbp.conf to:

    [DEFAULT]
    builder = sbuild

to use sbuild rather than pbuilder.


To make a new release:

    git checkout debian
    gbp import-orig --uscan  # Scans and downloads the new source.
    gbp dch --snapshot --auto debian
    gbp buildpackage --git-ignore-new -c <schroot name> -A -s -d wheezy

If the build succeeds then it will have placed a .deb file in the directory
above. It is a good idea to check that is installable by copying it to the
schroot and installing via:

    dpkg -i <name>.deb
    apt-get install -f

If it works (and runs) then we can actually release it:

    gbp dch --release --auto  # Ensure that the changelog doesnt lie
    git commit -m "<RELEASE>" debian/changelog
    git clean -dfx  # This ensures that there are no uncommitted changes
    gbp buildpackage --git-tag -c <schroot name> -A -s -d wheezy

To push to the repo:

    debsign
    debrelease matrix-synapse

