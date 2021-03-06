#!/usr/bin/env python2
#
'''
1. This script will either:
  - bootstrap a new or an existing cluster
  - join/rejoin an existing cluster
2. Requirements (normally installed thru puppet):
  - yum install python-argparse MySQL-python
3. Avoid joining all nodes at once
4. The paramter file will be stored in /root/ as it contains DB credentials

Bugs & Workarounds:
1.  We have a bug in Innobackupex:
      - https://bugs.launchpad.net/percona-xtrabackup/+bug/1272329
    A possible solution can come here:
      - https://bugs.launchpad.net/percona-xtrabackup/2.2/+bug/688717
    I prefear using the default directory rather than moving to a subdirectory.
    Therefore we workaround the issue by letting puppet install an incron
    entry that immediately reassign the directory ownership to mysql:mysql

TODO: (see TODO.txt)

Author: Massimiliano Adamo <maxadamo@gmail.com>
'''

import subprocess, argparse, textwrap, MySQLdb, shutil, signal
import platform, socket, time, glob, sys, pwd, grp, os

PURPLE = '\033[95m'
BLUE = '\033[94m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
WHITE = '\033[0m'
galera_params = "/root/galera_params.py"
if not os.access(galera_params, os.F_OK):
    print(RED + "Please check if " + galera_params + " exists " +
          "and you are running this script as root" + WHITE)
    sys.exit(1)
execfile(galera_params)

myname = socket.gethostname().split(".", 1)[0] + "." + mydomain
other_nodes = list(all_nodes)
other_nodes.remove(myname)
other_wsrep = []
remaining_nodes = []
lastcheck_nodes = []
for item in other_nodes:
    other_wsrep.append(item)
DATADIR = "/var/lib/mysql"
try:
    myuid = pwd.getpwnam("mysql").pw_uid
except KeyError:
    print("I can't find the user mysql \nGiving up...")
    sys.exit(1)
try:
    mygid = grp.getgrnam("mysql").gr_gid
except KeyError:
    print("I can't find the group mysql \nGiving up...")
    sys.exit(1)
rpm_systems = ['fedora', 'redhat', 'centos']
deb_systems = ['debian', 'Ubuntu', 'LinuxMint']
this_system = platform.dist()[0]


def clean_underlying_dir():
    """cleanup directory under /var/lib/mysql"""
    if os.path.ismount("DATADIR"):
        unmount = subprocess.Popen(["/bin/umount", DATADIR])
        out, err = unmount.communicate()
        retcode = unmount.poll()
        if retcode == 1:
            fuser = subprocess.Popen(["/sbin/fuser", "-uvm", DATADIR],
                                        stdout=subprocess.PIPE)
            fuser.communicate()
            print(RED
              + "\nSome process is not allowing to umount: "
              + WHITE + DATADIR
              +"\n\nPlease check it manually\n")
            sys.exit(1)
    for sqldiritem in glob.glob(DATADIR + "/*"):
        if os.path.isdir(sqldiritem):
            shutil.rmtree(sqldiritem)
        else:
            os.unlink(sqldiritem)
    mount = subprocess.Popen(['/bin/mount', DATADIR,],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    out, err = mount.communicate()


def kill_mysql():
    """kill mysql"""
    print("\nKilling any existing instance of MySQL\n")
    mysqlproc = subprocess.Popen(['pgrep', '-f', 'mysqld'],
                                stdout=subprocess.PIPE)
    out, err = mysqlproc.communicate()
    for pid in out.splitlines():
        os.kill(int(pid), signal.SIGKILL)
    if os.path.isfile("/var/lock/subsys/mysql"):
        os.unlink("/var/lock/subsys/mysql")
    if os.path.ismount(DATADIR):
        time.sleep(3)
        clean_underlying_dir()


def fill_files():
    """fill data inside config files"""
    check_vendor()
    docdir = "/usr/share/doc/galera-wizard"
    nagios_cnf = "/etc/my_nagios.cnf"
    nagios_chk = "/usr/local/bin/galeracheck.sh"
    if vendor == "mariadb":
        source_config = docdir + "/server.cnf.MariaDB"
        config_file = "/etc/my.cnf.d/server.cnf.example"
    elif vendor == "percona":
        source_config = docdir + "/my.cnf.Percona"
        config_file = "/etc/my.cnf.example"
    f = open(source_config, 'r')
    lines_config = f.readlines()
    f.close()
    ff = open(config_file, 'wb')
    for line in source_config:
        if "wsrep_cluster_address=gcomm://" in line:
            f.write("wsrep_cluster_address=gcomm://" + ','.join(str(p) for p in all_nodes))
        if "wsrep_sst_receive_address=" in line:
            f.write("wsrep_sst_receive_address=" + myname)
        if "wsrep_sst_auth=sstuser:" in line:
            f.write("wsrep_sst_auth=sstuser:" + credentials["nagios"])
    ff.close()
    print("\nCreated file: " + config_file)
    f = open(docdir + "/my_nagios.cnf.example", 'r')
    lines_cnf = f.readlines()
    f.close()
    f = open(nagios_chk, 'r')
    lines_chk = f.readlines()
    f.close()
    f = open(nagios_chk, 'wb')
    for line in lines_chk:
        if "NODE_COUNT=" in line:
            f.write("NODE_COUNT=" + str(len(all_nodes)) + "\n")
        else:
            f.write(line)
    f.close()
    print("Created file: " + nagios_chk)
    f = open(nagios_cnf, 'wb')
    for line in lines_cnf:
        if "password=" in line:
            f.write("password=" + credentials["nagios"] + "\n")
        else:
            f.write(line)
    f.close()
    print("Created file: " + nagios_cnf + "\n")


def rename_mycnf():
    """rename /root/.my.cnf"""
    if os.path.isfile("/root/.my.cnf"):
        os.rename("/root/.my.cnf", "/root/.my.cnf.bak")


def restore_mycnf():
    """restore /root/.my.cnf"""
    if os.path.isfile("/root/.my.cnf.bak"):
        os.rename("/root/.my.cnf.bak", "/root/.my.cnf")


def check_vendor():
    """check if it is Percona or MariaDB"""
    global bootstrap_cmd, vendor
    for mysystem in rpm_systems + deb_systems:
        if this_system in rpm_systems:
            import yum
            found = "yum"
        elif mysystem in deb_systems:
            import apt
            found = "apt"
    print("\n" + platform.dist()[0] + " " + platform.dist()[1] + " detected...")
    if found == "apt":
        import apt
        cache = apt.Cache()
        if cache['percona-xtradb-cluster-server'].is_installed:
            bootstrap_cmd = "bootstrap-pxc"
            vendor = "percona"
        elif ['mariadb-galera-server'] in cache.keys():
            if cache['mariadb-galera-server'].is_installed:
                bootstrap_cmd = "bootstrap"
                vendor = "mariadb"
        else:
            print("You don't have neither mariadb-galera-server or percona on "
                  + this_system + "\nGiving up... ")
            sys.exit(1)
    elif found == "yum":
        oldstdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        yb = yum.YumBase()
        if yb.rpmdb.searchNevra(name='MariaDB-Galera-server') or yb.rpmdb.searchNevra(name='MariaDB-server'):
            bootstrap_cmd = "bootstrap"
            vendor = "mariadb"
        elif yb.rpmdb.searchNevra(name='Percona-XtraDB-Cluster-full-56'):
            bootstrap_cmd = "bootstrap-pxc"
            vendor = "percona"
        else:
            sys.stdout = oldstdout
            print(RED + "I do not see neither MariaDB or Percona installed" + WHITE)
            sys.exit(1)
        sys.stdout = oldstdout
    else:
        print (this_system + " is not supported")
        sys.exit


def initialize_mysql():
    """initialize mysql default schemas"""
    for sqldiritem in glob.glob(DATADIR + "/*"):
        if os.path.isdir(sqldiritem):
            shutil.rmtree(sqldiritem)
        else:
            os.unlink(sqldiritem)
    try:
        subprocess.call("/usr/bin/mysql_install_db")
    except:
        print("Error creating initial schemas")
        sys.exit(1)


def bootstrap_mysql(boot):
    """bootstrap the cluster"""
    check_vendor()
    if boot == "new":
        rename_mycnf()
    try:
        subprocess.call(["/etc/rc.d/init.d/mysql", bootstrap_cmd])
    except:
        print("Error bootstrapping the cluster")
        sys.exit(1)
    if boot == "new":
        try:
            subprocess.call(["/usr/bin/mysqladmin",
                             "--no-defaults",
                             "-u", "root", "password",
                             credentials["root"]])
        except:
            print("Error setting root password")
        restore_mycnf()


def checkhost(mysqlhost):
    """check the socket on the other nodes"""
    print("\nChecking socket on " + mysqlhost + " ...")
    FNULL = open(os.devnull, 'w')
    ping = subprocess.Popen(["/bin/ping", "-w2", "-c2", mysqlhost],
        stdout=FNULL, stderr=subprocess.STDOUT)
    out, err = ping.communicate()
    retcode = ping.poll()
    if retcode != 0:
        print(RED + "Skipping " + mysqlhost + ": ping failed" + WHITE)
        other_wsrep.remove(mysqlhost)
    else:
        cnx_mysqlhost = None
        try:
            cnx_mysqlhost = MySQLdb.connect(user='sstuser',
                                        passwd=credentials["sstuser"],
                                        host=mysqlhost)
        except MySQLdb.Error:
            print(YELLOW + "Skipping " + mysqlhost + ": socket is down" + WHITE)
            other_wsrep.remove(mysqlhost)
        else:
            print(GREEN + "Socket on " + mysqlhost + " is up" + WHITE)
        finally:
            if cnx_mysqlhost:
                cnx_mysqlhost.close()


def checkwsrep(mysqlhost):
    """check if the other nodes belong to the cluster"""
    FNULL = open(os.devnull, 'w')
    ping = subprocess.Popen(["/bin/ping", "-w2", "-c2", mysqlhost],
        stdout=FNULL, stderr=subprocess.STDOUT)
    out, err = ping.communicate()
    retcode = ping.poll()
    FNULL.close()
    if retcode == 0:
        print("\nChecking if " + mysqlhost + " belongs to cluster ...")
        cnx_mysqlhost = None
        wsrep_status = 0
        try:
            cnx_mysqlhost = MySQLdb.connect(user='sstuser',
                                        passwd=credentials["sstuser"],
                                        host=mysqlhost)
            cursor = cnx_mysqlhost.cursor()
            wsrep_status = cursor.execute("""
                                SELECT VARIABLE_VALUE
                                    from information_schema.GLOBAL_STATUS
                                    where VARIABLE_VALUE = 'ON'
                                    AND VARIABLE_NAME LIKE 'wsrep_ready'
                                """)
        except:
            pass
        finally:
            if cnx_mysqlhost:
                cnx_mysqlhost.close()
        if wsrep_status == 1:
            lastcheck_nodes.append(mysqlhost)
            print(GREEN + mysqlhost + " belongs to the cluster." + WHITE)
        else:
            print(YELLOW
                + "Skipping " + mysqlhost + ": it is not in the cluster."
                + WHITE)


def try_joining(how):
    """If we have nodes try Joining the cluster"""
    if how == "new":
        rename_mycnf()
    if not lastcheck_nodes:
        print(RED
              + "We don't have any host available in the Cluster.\n"
              + WHITE + "Either:\n"
              + "- None of the hosts has the value 'wsrep_ready' to 'ON'\n"
              + "- None of the host is running the MySQL process\n")
        sys.exit(1)
    else:
        print("Asking " + lastcheck_nodes[0] + " to gently join the cluster")
        try:
            subprocess.call(["/etc/rc.d/init.d/mysql",
                             "start",
                             "--wsrep_cluster_address=gcomm://" + lastcheck_nodes[0]])
        except:
            print(RED + "Unable to gently join the cluster" + WHITE)
            print("Force joining cluster with " + lastcheck_nodes[0])
            if os.path.isfile(DATADIR + "/grastate.dat"):
                os.unlink(DATADIR + "/grastate.dat")
                try:
                    subprocess.call(["/etc/rc.d/init.d/mysql",
                                 "start",
                                 "--wsrep_cluster_address=gcomm://" + lastcheck_nodes[0]])
                except:
                    print(RED + "Unable to join the cluster" + WHITE)
                    sys.exit(1)
                else:
                    restore_mycnf()
            else:
                restore_mycnf()
                print(RED + "Unable to join the cluster" + WHITE)
                sys.exit(1)
        else:
            restore_mycnf()


def show_statements():
    """Show SQL statements to create all stuff"""
    os.system('clear')
    all_nodes.append("localhost")
    print("\n# remove anonymous\nDROP USER ''@'localhost'")
    print("DROP USER ''@'" + myname + "'")
    print("\n# create nagios table\nCREATE DATABASE IF NOT EXIST `test`;")
    print("CREATE TABLE `test`.`nagios` ( `id` varchar(255) DEFAULT NULL )"
          + " ENGINE=InnoDB DEFAULT CHARSET=utf8;")
    print("INSERT INTO test.nagios SET id=(\"placeholder\");")
    for thisuser in ['root', 'sstuser', 'nagios']:
        print("\n# define user " + thisuser)
        if thisuser is "root":
            for onthishost in ["localhost", "127.0.0.1", "::1"]:
                print("set PASSWORD for 'root'@'" + onthishost
                    + "' = PASSWORD('" + credentials[thisuser] + "')")
        for thishost in all_nodes:
            if thisuser is not "root":
                print("CREATE USER '" + thisuser + "'@'" + thishost
                      + "' IDENTIFIED BY '" + credentials[thisuser] + "';")
        for thishost in all_nodes:
            if thisuser is "sstuser":
                thisgrant = "RELOAD, LOCK TABLES, REPLICATION CLIENT ON *.*"
            elif thisuser is "nagios":
                thisgrant = "UPDATE ON test.nagios"
            if thisuser is not "root":
                print("GRANT " + thisgrant + " TO '"
                      + thisuser + "'@'" + thishost + "';")
    print("")


def create_nagios_table():
    """create test table for nagios"""
    cnx_local_test = MySQLdb.connect(user='root',
                                     passwd=credentials["root"],
                                     host='localhost',
                                     db='test')
    cursor = cnx_local_test.cursor()
    print("\nCreating table for Nagios\n")
    try:
        cursor.execute("""
                    CREATE TABLE `nagios` (
                        `id` varchar(255) DEFAULT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8
                    """)
        cnx_local_test.commit()
    except:
        print("Unable to create test table")
        sys.exit(1)
    try:
        cursor.execute("""
                    INSERT INTO test.nagios SET id=("placeholder");
                    """)
        cnx_local_test.commit()
    except:
        print("Unable to write to test table")
    finally:
        if cnx_local_test:
            cnx_local_test.close()


def create_users(thisuser):
    """create users root, nagios and SST and delete anonymous"""
    cnx_local = MySQLdb.connect(user='root',
                                passwd=credentials["root"],
                                host='localhost')
    cursor = cnx_local.cursor()
    try:
        cursor.execute("""DROP USER ''@'localhost'""")
    except:
        pass
    try:
        cursor.execute("""DROP USER ''@'%s'""" % (myname))
    except:
        pass
    print("Creating user: " + thisuser)
    if thisuser is "root":
        for onthishost in ["localhost", "127.0.0.1", "::1"]:
            cursor.execute("""
                set PASSWORD for 'root'@'%s' = PASSWORD('%s')
                """ % (onthishost, credentials[thisuser]))
    for thishost in all_nodes:
        if thisuser is "sstuser":
            thisgrant = "RELOAD, LOCK TABLES, REPLICATION CLIENT ON *.*"
        elif thisuser is "nagios":
            thisgrant = "UPDATE ON test.nagios"
        if thisuser is not "root":
            try:
                cursor.execute("""
                    CREATE USER '%s'@'%s' IDENTIFIED BY '%s'
                    """ % (thisuser, thishost, credentials[thisuser]))
            except:
                print("Unable to create user " + thisuser + " on " + thishost)
            try:
                cursor.execute("""
                        GRANT %s TO '%s'@'%s'
                        """ % (thisgrant, thisuser, thishost))
            except:
                print("Unable to set permission to " + thisuser
                      + " at " + thishost)
    if cnx_local:
        cursor.execute("""FLUSH PRIVILEGES""")
        cnx_local.close()


def createcluster(mode):
    """create and bootstrap a cluster"""
    fill_files()
    os.chown(DATADIR, myuid, mygid)
    for hostitem in other_nodes:
        checkhost(hostitem)
    if other_wsrep:
        for wsrepitem in other_wsrep:
            remaining_nodes.append(wsrepitem)
    if remaining_nodes:
        alive = str(remaining_nodes)[1:-1]
        print(RED
           + "\nThe following nodes from the same cluster are already running:\n"
           + WHITE + alive
           + "\n\nTo boostrap a new cluster you need to switch them off\n")
    else:
        kill_mysql()
        if mode == "new":
            initialize_mysql()
        bootstrap_mysql(mode)
        if mode == "new":
            create_nagios_table()
            all_nodes.append("localhost")
            for creditem in credentials.keys():
                create_users(creditem)
            print("")


def joincluster(model):
    """join a cluster"""
    os.chown(DATADIR, myuid, mygid)
    fill_files()
    for hostitem in other_nodes:
        checkhost(hostitem)
    if other_wsrep:
        for wsrepitem in other_wsrep:
            remaining_nodes.append(wsrepitem)
    if remaining_nodes:
        for wsrephost in other_wsrep:
            checkwsrep(wsrephost)
    if lastcheck_nodes:
        kill_mysql()
        if model == "new":
            initialize_mysql()
    try_joining(model)


def checkonly():
    """runs a cluster check"""
    other_wsrep.append(myname)
    for hostitem in all_nodes:
        checkhost(hostitem)
    if other_wsrep:
        for wsrepitem in other_wsrep:
            remaining_nodes.append(wsrepitem)
    if remaining_nodes:
        for wsrephost in other_wsrep:
            checkwsrep(wsrephost)


def main():
    """Parse options thru argparse and run it..."""
    intro = '''\
         Use this script to bootstrap, join or re-join nodes within a Galera Cluster
         ---------------------------------------------------------------------------
           Avoid joining more than one node at once!
         '''
    parser = argparse.ArgumentParser(
        formatter_class=lambda prog:
        argparse.RawDescriptionHelpFormatter(prog,max_help_position=29),
        description=textwrap.dedent(intro),
        epilog="Author: Massimiliano Adamo <maxadamo@gmail.com")
    parser.add_argument('-cc', '--create-config', help='create config samples',
                        action='store_true', dest='fill_files()', required=False)
    parser.add_argument('-cg', '--check-galera', help='check if all nodes are healthy',
                        action='store_true', dest='checkonly()', required=False)
    parser.add_argument('-dr', '--dry-run', help='show SQL statements to run on this cluster',
                        action='store_true', dest='show_statements()', required=False)
    parser.add_argument('-je', '--join-existing', help='join existing Cluster',
                        action='store_true', dest='joincluster("existing")', required=False)
    parser.add_argument('-be', '--bootstrap-existing', help='bootstrap existing Cluster',
                        action='store_true', dest='createcluster("existing")', required=False)
    parser.add_argument('-jn', '--join-new', help='join existing Cluster (DESTROY DATA)',
                        action='store_true', dest='joincluster("new")', required=False)
    parser.add_argument('-bn', '--bootstrap-new', help='bootstrap new Cluster (DESTROY DATA)',
                        action='store_true', dest='createcluster("new")', required=False)
    args = parser.parse_args()
    argsdict = vars(args)

    if not any(argsdict.values()):
        parser.error('\n\tNo arguments provided.\n\tUse -h, --help for help')
    else:
        for key in list(argsdict.keys()):
            if argsdict[str(key)] is True:
                eval(key)


# Here we Go.
if __name__ == "__main__":
    main()
