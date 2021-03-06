# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""
Prepare and start different analysis types
"""
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import copy
from multiprocessing.managers import SyncManager
import os
import shlex
import shutil
import signal
import subprocess
import time

from libcodechecker.logger import get_logger
from libcodechecker.analyze import analysis_manager
from libcodechecker.analyze import analyzer_env
from libcodechecker.analyze import pre_analysis_manager
from libcodechecker.analyze.analyzers import analyzer_types


LOG = get_logger('analyzer')


def prepare_actions(actions, enabled_analyzers):
    """
    Set the analyzer type for each buildaction.
    Multiple actions if multiple source analyzers are set.
    """
    res = []

    for ea in enabled_analyzers:
        for action in actions:
            new_action = copy.deepcopy(action)
            new_action.analyzer_type = ea
            res.append(new_action)
    return res


def create_actions_map(actions, manager):
    """
    Create a dict for the build actions which is shareable
    safely between processes.
    Key: (source_file, target)
    Value: BuildAction
    """

    result = manager.dict()

    for act in actions:
        if act.source_count > 1:
            LOG.debug("Multiple sources for one build action: " +
                      str(act.sources))
        source = os.path.join(act.directory, next(act.sources))
        key = source, act.target
        if key in result:
            LOG.debug("Multiple entires in compile database "
                      "with the same (source, target) pair: (%s, %s)" % key)
        result[key] = act
    return result


def __get_analyzer_version(context, analyzer_config_map):
    """
    Get the path and the version of the analyzer binaries.
    """
    check_env = analyzer_env.get_check_env(context.path_env_extra,
                                           context.ld_lib_path_extra)

    # Get the analyzer binaries from the config_map which
    # contains only the checked and available analyzers.
    versions = {}
    for _, analyzer_cfg in analyzer_config_map.items():
        analyzer_bin = analyzer_cfg.analyzer_binary
        version = [analyzer_bin, u' --version']
        try:
            output = subprocess.check_output(shlex.split(' '.join(version)),
                                             env=check_env)
            versions[analyzer_bin] = output
        except (subprocess.CalledProcessError, OSError) as oerr:
            LOG.warning("Failed to get analyzer version: " + ' '.join(version))
            LOG.warning(oerr.strerror)

    return versions


def __mgr_init():
    """
    This function is set for the SyncManager object which handles shared data
    structures among the processes of the pool. Ignoring the SIGINT signal is
    necessary in the manager object so it doesn't terminate before the
    termination of the process pool.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def __get_statistics_data(args, manager):
    statistics_data = None

    if 'stats_enabled' in args and args.stats_enabled:
        statistics_data = manager.dict({
            'stats_out_dir': os.path.join(args.output_path, "stats")})

    if 'stats_output' in args and args.stats_output:
        statistics_data = manager.dict({'stats_out_dir':
                                        args.stats_output})

    if 'stats_min_sample_count' in args and statistics_data:
        if args.stats_min_sample_count > 1:
            statistics_data['stats_min_sample_count'] =\
                args.stats_min_sample_count
        else:
            LOG.error("stats_min_sample_count"
                      "must be greater than 1.")
            return None

    if 'stats_relevance_threshold' in args and statistics_data:
        if 1 > args.stats_relevance_threshold > 0:
            statistics_data['stats_relevance_threshold'] =\
                args.stats_relevance_threshold
        else:
            LOG.error("stats-relevance-threshold must be"
                      " greater than 0 and smaller than 1.")
            return None

    return statistics_data


def perform_analysis(args, skip_handler, context, actions, metadata):
    """
    Perform static analysis via the given (or if not, all) analyzers,
    in the given analysis context for the supplied build actions.
    Additionally, insert statistical information into the metadata dict.
    """

    analyzers = args.analyzers if 'analyzers' in args \
        else analyzer_types.supported_analyzers
    analyzers, _ = analyzer_types.check_supported_analyzers(
        analyzers, context)

    ctu_collect = False
    ctu_analyze = False
    ctu_dir = ''
    if 'ctu_phases' in args:
        ctu_dir = os.path.join(args.output_path, 'ctu-dir')
        args.ctu_dir = ctu_dir
        if analyzer_types.CLANG_SA not in analyzers:
            LOG.error("CTU can only be used with the clang static analyzer.")
            return
        ctu_collect = args.ctu_phases[0]
        ctu_analyze = args.ctu_phases[1]

    if 'stats_enabled' in args and args.stats_enabled:
        if analyzer_types.CLANG_SA not in analyzers:
            LOG.debug("Statistics can only be used with "
                      "the Clang Static Analyzer.")
            return

    actions = prepare_actions(actions, analyzers)
    config_map = analyzer_types.build_config_handlers(args, context, analyzers)

    # Save some metadata information.
    versions = __get_analyzer_version(context, config_map)
    metadata['versions'].update(versions)

    metadata['checkers'] = {}
    for analyzer in analyzers:
        metadata['checkers'][analyzer] = []

        for check, data in config_map[analyzer].checks().items():
            enabled, _ = data
            if not enabled:
                continue
            metadata['checkers'][analyzer].append(check)

    if ctu_collect:
        shutil.rmtree(ctu_dir, ignore_errors=True)
    elif ctu_analyze and not os.path.exists(ctu_dir):
        LOG.error("CTU directory:'" + ctu_dir + "' does not exist.")
        return

    start_time = time.time()

    # Use Manager to create data objects which can be
    # safely shared between processes.
    manager = SyncManager()
    manager.start(__mgr_init)

    config_map = manager.dict(config_map)
    actions_map = create_actions_map(actions, manager)

    # Setting to not None value will enable statistical analysis features.
    statistics_data = __get_statistics_data(args, manager)

    if ctu_collect or statistics_data:
        ctu_data = None
        if ctu_collect or ctu_analyze:
            ctu_data = manager.dict({'ctu_dir': ctu_dir,
                                     'ctu_func_map_file': 'externalFnMap.txt',
                                     'ctu_temp_fnmap_folder':
                                     'tmpExternalFnMaps'})

        pre_analyze = [a for a in actions
                       if a.analyzer_type == analyzer_types.CLANG_SA]
        pre_analysis_manager.run_pre_analysis(pre_analyze,
                                              context,
                                              config_map,
                                              args.jobs,
                                              skip_handler,
                                              ctu_data,
                                              statistics_data,
                                              manager)

    if 'stats_output' in args and args.stats_output:
        return

    if 'stats_dir' in args and args.stats_dir:
        statistics_data = manager.dict({'stats_out_dir': args.stats_dir})

    ctu_reanalyze_on_failure = 'ctu_reanalyze_on_failure' in args and \
        args.ctu_reanalyze_on_failure

    if ctu_analyze or statistics_data or (not ctu_analyze and not ctu_collect):

        LOG.info("Starting static analysis ...")
        analysis_manager.start_workers(actions_map, actions, context,
                                       config_map, args.jobs,
                                       args.output_path,
                                       skip_handler,
                                       metadata,
                                       'quiet' in args,
                                       'capture_analysis_output' in args,
                                       args.timeout if 'timeout' in args
                                       else None,
                                       ctu_reanalyze_on_failure,
                                       statistics_data,
                                       manager)
        LOG.info("Analysis finished.")
        LOG.info("To view results in the terminal use the "
                 "\"CodeChecker parse\" command.")
        LOG.info("To store results use the \"CodeChecker store\" command.")
        LOG.info("See --help and the user guide for further options about"
                 " parsing and storing the reports.")
        LOG.info("----=================----")

    end_time = time.time()
    LOG.info("Analysis length: " + str(end_time - start_time) + " sec.")

    metadata['timestamps'] = {'begin': start_time,
                              'end': end_time}

    if ctu_collect and ctu_analyze:
        shutil.rmtree(ctu_dir, ignore_errors=True)

    manager.shutdown()
