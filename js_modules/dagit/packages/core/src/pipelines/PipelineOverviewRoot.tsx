import * as React from 'react';
import {useHistory, useLocation, useParams} from 'react-router-dom';

import {useTrackPageView} from '../app/analytics';
import {tokenForAssetKey} from '../asset-graph/Utils';
import {AssetLocation} from '../asset-graph/useFindAssetLocation';
import {assetDetailsPathForKey} from '../assets/assetDetailsPathForKey';
import {isThisThingAJob, useRepository} from '../workspace/WorkspaceContext';
import {RepoAddress} from '../workspace/types';
import {workspacePathFromAddress} from '../workspace/workspacePath';

import {PipelineExplorerContainer} from './PipelineExplorerRoot';
import {
  explorerPathFromString,
  explorerPathToString,
  ExplorerPath,
  useStripSnapshotFromPath,
} from './PipelinePathUtils';
import {useJobTitle} from './useJobTitle';

interface Props {
  repoAddress: RepoAddress;
}

export const PipelineOverviewRoot: React.FC<Props> = (props) => {
  useTrackPageView();

  const {repoAddress} = props;
  const history = useHistory();
  const location = useLocation();
  const params = useParams();

  const explorerPath = explorerPathFromString(params['0']);

  const repo = useRepository(repoAddress);
  const isJob = isThisThingAJob(repo, explorerPath.pipelineName);

  useJobTitle(explorerPath, isJob);
  useStripSnapshotFromPath({pipelinePath: explorerPathToString(explorerPath)});

  const onChangeExplorerPath = React.useCallback(
    (path: ExplorerPath, action: 'push' | 'replace') => {
      history[action]({
        search: location.search,
        pathname: workspacePathFromAddress(
          repoAddress,
          `/${isJob ? 'jobs' : 'pipelines'}/${explorerPathToString(path)}`,
        ),
      });
    },
    [history, location.search, repoAddress, isJob],
  );

  const onNavigateToSourceAssetNode = React.useCallback(
    (node: AssetLocation) => {
      if (!node.jobName || !node.opNames.length || !node.repoAddress) {
        // This op has no definition in any loaded repository (source asset).
        // The best we can do is show the asset page. This will still be mostly empty,
        // but there can be a description.
        history.push(assetDetailsPathForKey(node.assetKey, {view: 'definition'}));
        return;
      }

      // Note: asset location can be in another job AND in another repo! Need
      // to build a full job URL using the `node` info here.
      history.replace({
        search: location.search,
        pathname: workspacePathFromAddress(
          node.repoAddress,
          `/jobs/${explorerPathToString({
            ...explorerPath,
            opNames: [tokenForAssetKey(node.assetKey)],
            opsQuery: '',
            pipelineName: node.jobName!,
          })}`,
        ),
      });
    },
    [explorerPath, history, location.search],
  );

  return (
    <PipelineExplorerContainer
      repoAddress={repoAddress}
      explorerPath={explorerPath}
      onChangeExplorerPath={onChangeExplorerPath}
      onNavigateToSourceAssetNode={onNavigateToSourceAssetNode}
    />
  );
};
