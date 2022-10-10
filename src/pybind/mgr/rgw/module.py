import json
import threading
import yaml
import errno
import base64
import functools

from mgr_module import MgrModule, CLICommand, HandleCommandResult
import orchestrator

from ceph.deployment.service_spec import RGWSpec, PlacementSpec
from typing import Any, Optional, Sequence, Iterator, List

from ceph.rgw.types import RGWAMException, RGWAMEnvMgr, RealmToken
from ceph.rgw.rgwam_core import EnvArgs, RGWAM
from orchestrator import OrchestratorClientMixin, OrchestratorError


class OrchestratorAPI(OrchestratorClientMixin):
    def __init__(self, mgr):
        super(OrchestratorAPI, self).__init__()
        self.set_mgr(mgr)  # type: ignore

    def status(self):
        try:
            status, message, _module_details = super().available()
            return dict(available=status, message=message)
        except (RuntimeError, OrchestratorError, ImportError) as e:
            return dict(available=False, message=f'Orchestrator is unavailable: {e}')

class RGWAMOrchMgr(RGWAMEnvMgr):
    def __init__(self, mgr):
        self.mgr = mgr

    def tool_exec(self, prog, args):
        cmd = [prog] + args
        rc, stdout, stderr = self.mgr.tool_exec(args=cmd)
        return cmd, rc, stdout, stderr

    def apply_rgw(self, spec):
        completion = self.mgr.apply_rgw(spec)
        orchestrator.raise_if_exception(completion)

    def list_daemons(self, service_name, daemon_type=None, daemon_id=None, host=None, refresh=True):
        completion = self.mgr.list_daemons(service_name,
                                           daemon_type,
                                           daemon_id=daemon_id,
                                           host=host,
                                           refresh=refresh)
        return orchestrator.raise_if_exception(completion)


class Module(orchestrator.OrchestratorClientMixin, MgrModule):
    MODULE_OPTIONS = []

    # These are "native" Ceph options that this module cares about.
    NATIVE_OPTIONS = []

    def __init__(self, *args: Any, **kwargs: Any):
        self.inited = False
        self.lock = threading.Lock()
        super(Module, self).__init__(*args, **kwargs)
        self.api = OrchestratorAPI(self)

        # ensure config options members are initialized; see config_notify()
        self.config_notify()

        with self.lock:
            self.inited = True
            self.env = EnvArgs(RGWAMOrchMgr(self))

        # set up some members to enable the serve() method and shutdown()
        self.run = True
        self.event = threading.Event()

    def config_notify(self) -> None:
        """
        This method is called whenever one of our config options is changed.
        """
        # This is some boilerplate that stores MODULE_OPTIONS in a class
        # member, so that, for instance, the 'emphatic' option is always
        # available as 'self.emphatic'.
        for opt in self.MODULE_OPTIONS:
            setattr(self,
                    opt['name'],
                    self.get_module_option(opt['name']))
            self.log.debug(' mgr option %s = %s',
                           opt['name'], getattr(self, opt['name']))
        # Do the same for the native options.
        for opt in self.NATIVE_OPTIONS:
            setattr(self,
                    opt,
                    self.get_ceph_option(opt))
            self.log.debug(' native option %s = %s', opt, getattr(self, opt))

    def check_orchestrator():
        def inner(func):
            @functools.wraps(func)
            def wrapper(self, *args, **kwargs):
                available = self.api.status()['available']
                if available:
                    return func(self, *args, **kwargs)
                else:
                    err_msg = f"Cephadm is not available. Please enable cephadm by 'ceph mgr module enable cephadm'."
                    return HandleCommandResult(retval=-errno.EINVAL, stdout='', stderr=err_msg)
            return wrapper
        return inner

    @CLICommand('rgw admin', perm='rw')
    def _cmd_rgw_admin(self, params: Sequence[str]):
        """rgw admin"""
        cmd, returncode, out, err = self.env.mgr.tool_exec('radosgw-admin', params or [])

        self.log.error('retcode=%d' % returncode)
        self.log.error('out=%s' % out)
        self.log.error('err=%s' % err)

        return HandleCommandResult(retval=returncode, stdout=out, stderr=err)

    @CLICommand('rgw realm bootstrap', perm='rw')
    @check_orchestrator()
    def _cmd_rgw_realm_bootstrap(self,
                                 realm_name: Optional[str] = None,
                                 zonegroup_name: Optional[str] = None,
                                 zone_name: Optional[str] = None,
                                 port: Optional[int] = None,
                                 placement: Optional[str] = None,
                                 start_radosgw: Optional[bool] = True,
                                 inbuf: Optional[str] = None):
        """Bootstrap new rgw realm, zonegroup, and zone"""
        try:
            if inbuf:
                rgw_specs = self._parse_rgw_specs(inbuf)
            elif (realm_name and zonegroup_name and zone_name):
                placement_spec = PlacementSpec.from_string(placement) if placement else None
                rgw_specs = [RGWSpec(rgw_realm=realm_name,
                                     rgw_zonegroup=zonegroup_name,
                                     rgw_zone=zone_name,
                                     rgw_frontend_port=port,
                                     placement=placement_spec)]
            else:
                return HandleCommandResult(retval=-errno.EINVAL, stdout='', stderr='Invalid arguments: -h or --help for usage')

            for spec in rgw_specs:
                RGWAM(self.env).realm_bootstrap(spec, start_radosgw)

        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=0, stdout="Realm(s) created correctly. Please, use 'ceph rgw realm tokens' to get the token.", stderr='')

    def _parse_rgw_specs(self, inbuf: Optional[str] = None):
        """Parse RGW specs from a YAML file."""
        # YAML '---' document separator with no content generates
        # None entries in the output. Let's skip them silently.
        yaml_objs: Iterator = yaml.safe_load_all(inbuf)
        specs = [o for o in yaml_objs if o is not None]
        rgw_specs = []
        for spec in specs:
            # TODO(rkachach): should we use a new spec instead of RGWSpec here!
            rgw_spec = RGWSpec.from_json(spec)
            rgw_spec.validate()
            rgw_specs.append(rgw_spec)
        return rgw_specs

    @CLICommand('rgw realm zone-creds create', perm='rw')
    def _cmd_rgw_realm_new_zone_creds(self,
                                      realm_name: Optional[str] = None,
                                      endpoints: Optional[str] = None,
                                      sys_uid: Optional[str] = None):
        """Create credentials for new zone creation"""

        try:
            retval, out, err = RGWAM(self.env).realm_new_zone_creds(realm_name, endpoints, sys_uid)
        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=retval, stdout=out, stderr=err)

    @CLICommand('rgw realm zone-creds remove', perm='rw')
    def _cmd_rgw_realm_rm_zone_creds(self, realm_token: Optional[str] = None):
        """Create credentials for new zone creation"""

        try:
            retval, out, err = RGWAM(self.env).realm_rm_zone_creds(realm_token)
        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=retval, stdout=out, stderr=err)

    @CLICommand('rgw realm tokens', perm='r')
    def list_realm_tokens(self):
        realms_info = []
        for realm_info in RGWAM(self.env).get_realms_info():
            if not realm_info['master_zone_id']:
                realms_info.append({'realm': realm_info['realm_name'], 'token': 'realm has no master zone'})
            elif not realm_info['endpoint']:
                realms_info.append({'realm': realm_info['realm_name'], 'token': 'master zone has no endpoint'})
            elif not (realm_info['access_key'] and realm_info['secret']):
                realms_info.append({'realm': realm_info['realm_name'], 'token': 'master zone has no access/secret keys'})
            else:
                keys = ['realm_name', 'realm_id', 'is_primary', 'endpoint', 'access_key', 'secret']
                realm_token = RealmToken(**{k: realm_info[k] for k in keys})
                realm_token_b = realm_token.to_json().encode('utf-8')
                realm_token_s = base64.b64encode(realm_token_b).decode('utf-8')
                realms_info.append({'realm': realm_info['realm_name'], 'token': realm_token_s})

        return HandleCommandResult(retval=0, stdout=json.dumps(realms_info), stderr='')

    @CLICommand('rgw zone update', perm='rw')
    def update_zone_info(self, realm_name: str, zonegroup_name: str, zone_name: str, realm_token: str, endpoints: List[str]):
        try:
            retval, out, err = RGWAM(self.env).zone_modify(realm_name,
                                                           zonegroup_name,
                                                           zone_name,
                                                           endpoints,
                                                           realm_token)
            return (retval, 'Zone updated successfully', '')
        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

    @CLICommand('rgw zone create', perm='rw')
    @check_orchestrator()
    def _cmd_rgw_zone_create(self,
                             zone_name: Optional[str] = None,
                             realm_token: Optional[str] = None,
                             port: Optional[int] = None,
                             placement: Optional[str] = None,
                             start_radosgw: Optional[bool] = True,
                             inbuf: Optional[str] = None):
        """Bootstrap new rgw zone that syncs with existing zone"""
        try:
            if inbuf:
                rgw_specs = self._parse_rgw_specs(inbuf)
            elif (zone_name and realm_token):
                placement_spec = PlacementSpec.from_string(placement) if placement else None
                rgw_specs = [RGWSpec(rgw_realm_token=realm_token,
                                     rgw_zone=zone_name,
                                     rgw_frontend_port=port,
                                     placement=placement_spec)]
            else:
                return HandleCommandResult(retval=-errno.EINVAL, stdout='', stderr='Invalid arguments: -h or --help for usage')

            for rgw_spec in rgw_specs:
                retval, out, err = RGWAM(self.env).zone_create(rgw_spec, start_radosgw)
                if retval != 0:
                    break

        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=retval, stdout=out, stderr=err)

    @CLICommand('rgw zonegroup create', perm='rw')
    def _cmd_rgw_zonegroup_create(self,
                                  realm_token: Optional[str] = None,
                                  zonegroup_name: Optional[str] = None,
                                  endpoints: Optional[str] = None,
                                  zonegroup_is_master: Optional[bool] = True):
        """Bootstrap new rgw zonegroup"""

        try:
            retval, out, err = RGWAM(self.env).zonegroup_create(realm_token,
                                                                zonegroup_name,
                                                                endpoints,
                                                                zonegroup_is_master)
        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=retval, stdout=out, stderr=err)

    @CLICommand('rgw realm reconcile', perm='rw')
    def _cmd_rgw_realm_reconcile(self,
                                 realm_name: Optional[str] = None,
                                 zonegroup_name: Optional[str] = None,
                                 zone_name: Optional[str] = None,
                                 update: Optional[bool] = False):
        """Bootstrap new rgw zone that syncs with existing zone"""

        try:
            retval, out, err = RGWAM(self.env).realm_reconcile(realm_name, zonegroup_name,
                                                               zone_name, update)
        except RGWAMException as e:
            self.log.error('cmd run exception: (%d) %s' % (e.retcode, e.message))
            return (e.retcode, e.message, e.stderr)

        return HandleCommandResult(retval=retval, stdout=out, stderr=err)

    def shutdown(self) -> None:
        """
        This method is called by the mgr when the module needs to shut
        down (i.e., when the serve() function needs to exit).
        """
        self.log.info('Stopping')
        self.run = False
        self.event.set()
